"""Automated receipt check (Phase 8) — read the receipt, score it against the deal.

Runs in the background shortly after upload. ADVISORY ONLY: nothing here
approves, rejects or pays anything. It writes what it read and what it made of
it onto the Receipt row, so the admin reviewing a claim sees the receipt's
contents next to the photo instead of squinting at the photo alone.

Reading is done by Amazon Textract's AnalyzeExpense, which returns a receipt as
structured fields — vendor, total, subtotal, tax, date, receipt number — plus
the individual line items with their prices. Three reasons it suits this job:

  • It stays inside the AWS account the receipts already live in, in the same
    eu-west-2 region. No new data processor, nothing to add to the privacy
    policy, and no free tier quietly training on members' personal data.
  • 150 pages a month are free, which covers current volume outright.
  • It is not a language model, so a receipt with "APPROVE THIS CLAIM" printed
    on it is just text in a field. There is no instruction to follow.

The arithmetic is then done HERE, in Python, over the numbers Textract read:
line items against the subtotal, and subtotal plus tax against the total. That
is the part which catches fabricated receipts — generators reproduce the look
of a receipt but routinely get its sums wrong — and doing it as plain
subtraction is both cheaper and more reliable than asking a model to do mental
arithmetic it might hallucinate its way through.

Textract is told NOTHING about the deal; it only reports what is printed. Every
comparison against the campaign happens in `_score`, below.

Two known limits. Textract is tuned for paper receipts and invoices, so a
screenshot of an order-confirmation email may come back with few fields — that
surfaces as a low score with "no total was legible" rather than a wrong answer.
And this is not image forensics: a skilfully edited real receipt whose numbers
still add up will read as fine.
"""
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel
from sqlmodel import Session, select

from .cashback import parse_post_ts
from .db import engine
from .models import Campaign, Mention, Receipt
from .storage import read_receipt

# How far before the Instagram post a purchase may sit and still be plausible.
_MAX_DAYS_BEFORE_POST = 120

# What Textract accepts. Storage also allows .gif/.webp, which it does not —
# those surface as a readable error rather than an exception.
_TEXTRACT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf", ".tiff", ".tif"}

# Words that differ between how a shop brands itself and how it prints its
# legal name on a receipt ("Boots" vs "BOOTS UK LTD").
_NOISE_WORDS = {"ltd", "limited", "plc", "llp", "uk", "gb", "inc", "co",
                "company", "stores", "store", "group", "holdings", "the"}

# Summary fields we use, by Textract's normalised type name.
_VENDOR, _TOTAL, _SUBTOTAL, _TAX = "VENDOR_NAME", "TOTAL", "SUBTOTAL", "TAX"
_DATE, _RECEIPT_ID = "INVOICE_RECEIPT_DATE", "INVOICE_RECEIPT_ID"

# Tolerances for the two arithmetic checks, deliberately different.
#
# subtotal + tax = total comes entirely from the summary block, which Textract
# reads reliably, so it is held to the penny.
#
# The line-item sum is looser. Textract can miss a row, and receipts carry
# discounts, vouchers and deposits that never appear as line items — so a small
# discrepancy is normal on an honest receipt. Only a large one is worth
# reporting, or the check would flag half the legitimate uploads.
_SYMBOLS = {"GBP": "\u00a3", "USD": "$", "EUR": "\u20ac"}

_SUMMARY_TOLERANCE = 0.02
_LINE_ITEM_TOLERANCE_PCT = 0.10
_LINE_ITEM_TOLERANCE_MIN = 2.00


class ReceiptReading(BaseModel):
    """What the model reports seeing. Every field is an observation, not a verdict."""
    is_receipt: bool
    document_type: str                  # receipt | order_confirmation | invoice | other
    merchant_name: str
    total: Optional[float]
    currency: str                       # ISO code if printed, else ""
    purchase_date: str                  # YYYY-MM-DD, or "" if not legible
    order_number: str                   # order / transaction / receipt number as printed
    totals_add_up: Optional[bool]       # null when not checkable
    vat_consistent: Optional[bool]      # null when no VAT breakdown shown
    confidence: int                     # 0-100, how legible the document was
    concerns: list[str]


def _extension(image_key: str) -> str:
    return image_key[image_key.rfind("."):].lower() if "." in image_key else ""


def _money(text: str) -> Optional[float]:
    """'£12.34' / '1,234.56' / '$2.50' -> float. None if there's no number."""
    cleaned = re.sub(r"[^\d.,-]", "", text or "").replace(",", "")
    found = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(found.group()) if found else None


def _date(text: str) -> str:
    """A printed date -> 'YYYY-MM-DD'. '' if it can't be read confidently.

    DAY-FIRST on purpose: '03/08/2026' is 3 August on a UK receipt, not 3 March.
    Ambiguous slash dates are the one place this could silently misread, so the
    day-first formats are tried before the American ones and a plain ISO date
    (unambiguous) is tried first of all.
    """
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y",
                "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
                "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _summary(document: dict) -> dict:
    """Textract SummaryFields -> {TYPE: (text, confidence)}, best confidence wins."""
    fields = {}
    for field in document.get("SummaryFields", []):
        name = (field.get("Type") or {}).get("Text", "")
        value = field.get("ValueDetection") or {}
        text, confidence = value.get("Text", ""), value.get("Confidence", 0.0)
        if name and text and confidence >= fields.get(name, ("", -1.0))[1]:
            fields[name] = (text, confidence)
    return fields


def _currency(document: dict) -> str:
    """The ISO currency code Textract attached to the total, e.g. 'GBP'. '' if none."""
    for field in document.get("SummaryFields", []):
        if (field.get("Type") or {}).get("Text") == _TOTAL:
            return (field.get("Currency") or {}).get("Code", "") or ""
    return ""


def _line_item_prices(document: dict) -> list:
    """Every line item's PRICE, as floats."""
    prices = []
    for group in document.get("LineItemGroups", []):
        for item in group.get("LineItems", []):
            for field in item.get("LineItemExpenseFields", []):
                if (field.get("Type") or {}).get("Text") != "PRICE":
                    continue
                price = _money((field.get("ValueDetection") or {}).get("Text", ""))
                if price is not None:
                    prices.append(price)
    return prices


def _arithmetic(subtotal, tax, total, prices) -> tuple:
    """(subtotal+tax==total, line items sum right, concerns) — None where unknowable.

    Each answer is None rather than True when there isn't enough printed to
    check it. Saying "fine" about a sum we never did would quietly inflate the
    score of a receipt that simply didn't show its working.
    """
    concerns = []

    summary_ok = None
    if subtotal is not None and tax is not None and total is not None:
        summary_ok = abs((subtotal + tax) - total) <= _SUMMARY_TOLERANCE
        if not summary_ok:
            concerns.append(f"Subtotal {subtotal:.2f} plus tax {tax:.2f} "
                            f"is {subtotal + tax:.2f}, but the total reads {total:.2f}.")

    items_ok = None
    anchor = subtotal if subtotal is not None else total
    if prices and anchor is not None:
        summed = sum(prices)
        allowed = max(_LINE_ITEM_TOLERANCE_MIN, anchor * _LINE_ITEM_TOLERANCE_PCT)
        items_ok = abs(summed - anchor) <= allowed
        if not items_ok:
            concerns.append(f"The {len(prices)} item lines add up to {summed:.2f}, "
                            f"but the receipt says {anchor:.2f}.")
    elif not prices:
        concerns.append("No individual item lines could be read.")

    return summary_ok, items_ok, concerns


def _read(image_bytes: bytes, image_key: str) -> ReceiptReading:
    """Read one receipt with Textract. Raises on failure — the caller records it."""
    extension = _extension(image_key)
    if extension and extension not in _TEXTRACT_EXTENSIONS:
        raise RuntimeError(f"Textract cannot read '{extension}' files")

    import boto3  # lazy, as everywhere else AWS is touched

    response = boto3.client(
        "textract", region_name=os.environ.get("AWS_REGION", "eu-west-2"),
    ).analyze_expense(Document={"Bytes": image_bytes})

    documents = response.get("ExpenseDocuments") or []
    if not documents:
        return ReceiptReading(
            is_receipt=False, document_type="", merchant_name="", total=None,
            currency="", purchase_date="", order_number="", totals_add_up=None,
            vat_consistent=None, confidence=0,
            concerns=["Textract found no receipt or invoice in this image."])

    document = documents[0]
    fields = _summary(document)

    def text_of(name):
        return fields.get(name, ("", 0.0))[0]

    total = _money(text_of(_TOTAL))
    subtotal = _money(text_of(_SUBTOTAL))
    tax = _money(text_of(_TAX))
    prices = _line_item_prices(document)
    summary_ok, items_ok, concerns = _arithmetic(subtotal, tax, total, prices)

    raw_date = text_of(_DATE)
    purchase_date = _date(raw_date)
    if raw_date and not purchase_date:
        concerns.append(f"The date reads '{raw_date}', which could not be parsed.")

    # Confidence in what we actually used, not in the page as a whole.
    used = [fields[name][1] for name in (_VENDOR, _TOTAL, _DATE, _RECEIPT_ID)
            if name in fields]

    return ReceiptReading(
        is_receipt=bool(text_of(_VENDOR) or total is not None),
        document_type="receipt",
        merchant_name=text_of(_VENDOR),
        total=total,
        currency=_currency(document),
        purchase_date=purchase_date,
        order_number=text_of(_RECEIPT_ID).strip(),
        totals_add_up=items_ok,
        vat_consistent=summary_ok,
        confidence=int(sum(used) / len(used)) if used else 0,
        concerns=concerns,
    )


def _words(name: str) -> set:
    """Comparable words in a business name, minus the legal-suffix noise."""
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower())
            if w and w not in _NOISE_WORDS}


def _merchant_matches(printed: str, brand: str) -> bool:
    """Does the name on the receipt plausibly denote the deal's brand?"""
    a, b = _words(printed), _words(brand)
    return bool(a and b and (a & b))


def _required_spend(campaign: Optional[Campaign]) -> Optional[float]:
    """The £ a deal asks you to spend, read out of its spend_desc ('on a £100 spend')."""
    found = re.search(r"£\s*([\d,]+(?:\.\d{1,2})?)", campaign.spend_desc if campaign else "")
    return float(found.group(1).replace(",", "")) if found else None


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _score(reading: ReceiptReading, receipt: Receipt, campaign: Optional[Campaign],
           post_ts: Optional[datetime], session: Session) -> tuple:
    """Turn the reading into a 0-100 score plus the reasons behind it.

    Deliberately a pile of small deductions rather than one judgement: each is
    inspectable, and a reviewer can disagree with one without discarding the
    rest. Reasons come back worst-first.
    """
    if not reading.is_receipt:
        return 0, ["This does not appear to be a receipt or order confirmation."]

    findings = []                                    # (penalty, reason)

    if campaign and not _merchant_matches(reading.merchant_name, campaign.brand):
        findings.append((40, f"Merchant reads '{reading.merchant_name or 'unknown'}' "
                             f"but the deal is with {campaign.brand}."))

    if reading.totals_add_up is False:
        findings.append((30, "The printed totals don't add up."))
    if reading.vat_consistent is False:
        findings.append((20, "The printed VAT doesn't match the rate shown."))

    if reading.total is None:
        findings.append((10, "No total was legible."))
    else:
        required = _required_spend(campaign)
        if required is not None and reading.total + 0.005 < required:
            findings.append((25, f"Spend of {reading.total:.2f} is under the "
                                 f"£{required:.2f} this deal requires."))

    if not reading.purchase_date:
        findings.append((10, "No purchase date was legible."))
    elif post_ts:
        try:
            bought = datetime.strptime(reading.purchase_date, "%Y-%m-%d")
        except ValueError:
            findings.append((10, f"Purchase date '{reading.purchase_date}' is unreadable."))
        else:
            days = (_naive(post_ts) - bought).days
            if days < 0:
                findings.append((30, "The purchase is dated after the Instagram post."))
            elif days > _MAX_DAYS_BEFORE_POST:
                findings.append((15, f"The purchase is {days} days before the post."))

    if not reading.order_number:
        findings.append((10, "No order or transaction number was legible."))
    else:
        clash = session.exec(
            select(Receipt).where(Receipt.receipt_number == reading.order_number,
                                  Receipt.id != receipt.id)
        ).first()
        if clash is not None:
            findings.append((50, f"Order number {reading.order_number} is already "
                                 f"on claim #{clash.id}."))

    if reading.confidence < 50:
        findings.append((15, f"The document was hard to read (confidence "
                             f"{reading.confidence}%)."))

    findings.sort(key=lambda f: -f[0])
    reasons = [reason for _, reason in findings]
    reasons += [f"Noted: {c}" for c in reading.concerns[:5]]
    score = max(0, 100 - sum(penalty for penalty, _ in findings))
    return score, reasons


def check_receipt(receipt_id: int) -> None:
    """Read receipt `receipt_id` and record what we made of it. Never raises.

    Opens its own session: this runs as a background task, after the request
    that uploaded the receipt has already closed its own.
    """
    with Session(engine) as session:
        receipt = session.get(Receipt, receipt_id)
        if receipt is None:
            return

        try:
            image_bytes = read_receipt(receipt.image_key)
            if not image_bytes:
                raise RuntimeError("the stored receipt image could not be read")
            reading = _read(image_bytes, receipt.image_key)

            mention = session.get(Mention, receipt.post_id)
            campaign = (session.get(Campaign, receipt.campaign_id)
                        if receipt.campaign_id else None)
            score, reasons = _score(
                reading, receipt, campaign,
                parse_post_ts(mention.timestamp) if mention else None, session)

            receipt.check_status = "ok"
            receipt.check_score = score
            receipt.receipt_number = reading.order_number[:100]
            receipt.check_data = json.dumps(
                {"reading": reading.model_dump(), "reasons": reasons})
        except Exception as exc:  # noqa: BLE001 — a failed check must not break anything
            receipt.check_status = "error"
            receipt.check_score = 0
            receipt.check_data = json.dumps({"error": str(exc)[:500]})

        receipt.checked_at = datetime.utcnow()
        session.add(receipt)
        session.commit()


def summarise(receipt: Receipt) -> tuple:
    """(one-line summary, findings) for the admin queue. Never raises."""
    if receipt.check_status == "error":
        return "Automated check could not run.", []
    if receipt.check_status != "ok":
        return "", []
    try:
        data = json.loads(receipt.check_data)
    except ValueError:
        return "", []
    reading = data.get("reading", {})
    total = reading.get("total")
    parts = [reading.get("merchant_name") or "unknown merchant",
             _SYMBOLS.get(reading.get("currency", ""), "\u00a3") + f"{total:.2f}"
             if total is not None else None,
             reading.get("purchase_date") or None,
             f"order {reading['order_number']}" if reading.get("order_number") else None]
    return " · ".join(p for p in parts if p), data.get("reasons", [])
