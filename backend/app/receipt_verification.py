"""Claude-vision receipt verification (HITL).

Replaces app/verify.py's role as the AUTOMATIC background check on every
uploaded receipt. verify.py itself is untouched and still reachable manually
(POST /receipts/{id}/recheck, GET /receipts/admin/calibration) — nothing is
deleted, only unhooked from create_receipt's background task.

Two checks, computed in one Claude API call:
  • authenticity of origin — does this receipt plausibly come from the same
    merchant as their on-file reference receipt (store name/address/phone/
    VAT number, and visual format)?
  • purchase correctness — does it show a plausible purchase of the
    campaign's product, for a sane amount, on a sane date?

A human-in-the-loop threshold decides the claim's fate: a high-confidence
score can auto-approve and fire the (stub) cashback trigger immediately;
anything else — including a low score, a missing/failed call, or a
near-duplicate image — routes to the admin queue. A failure NEVER resolves
to auto-approval; see decide_verification_status.

IMPORTANT: `Receipt.verification_status` (this module's own field) is
descriptive metadata about the Claude decision. It does NOT by itself gate
cashback — `Receipt.status` (`cashback.py`'s clears_at/effective_status)
is what actually controls clearing, same as before this module existed. The
auto-approve path below sets BOTH, deliberately — see run_claude_verification.
"""
import io
import json
import os
import time
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session, select

from . import db
from .models import Campaign, Merchant, Receipt
from .storage import read_receipt
from .verify import _required_spend, apply_reading

# ── Config (env-read; this repo has no config module, matches instagram.py's
# APIFY_TOKEN pattern) ──
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
VERIFICATION_MODEL = os.environ.get("VERIFICATION_MODEL", "claude-sonnet-5")
IMAGE_HASH_THRESHOLD = int(os.environ.get("IMAGE_HASH_THRESHOLD", "5"))
VERIFICATION_AUTO_APPROVE_THRESHOLD = float(os.environ.get("VERIFICATION_AUTO_APPROVE_THRESHOLD", "0.85"))
# Kill switch. false = every claim routes to the admin queue regardless of
# score (the pipeline still runs and scores it, just never auto-decides).
AUTO_APPROVAL_ENABLED = os.environ.get("AUTO_APPROVAL_ENABLED", "false").lower() == "true"


class VerificationCallError(RuntimeError):
    """Raised when the Claude call cannot be completed (missing key, network
    failure after the retry, or a response that fails schema validation)."""


# ── Claude's JSON contract — every response is validated against these
# before any field is trusted. ──
class OcrFields(BaseModel):
    store_name: Optional[str] = None
    date: Optional[str] = None
    total: Optional[float] = None
    currency: Optional[str] = None
    line_items: list[str] = []


class AuthenticityResult(BaseModel):
    score: float = Field(ge=0, le=1)
    matched_fields: list[str] = []
    mismatches: list[str] = []
    reasoning: str = ""


class PurchaseMatchResult(BaseModel):
    score: float = Field(ge=0, le=1)
    product_match: str            # "yes" | "no" | "uncertain"
    amount_plausible: bool
    date_plausible: bool
    reasoning: str = ""


class ClaudeReceiptVerification(BaseModel):
    ocr_fields: OcrFields
    authenticity: AuthenticityResult
    purchase_match: PurchaseMatchResult
    red_flags: list[str] = []


class ReferenceExtraction(BaseModel):
    store_name: str = ""
    address: str = ""
    phone: str = ""
    vat_number: str = ""
    format_notes: str = ""
    confidence: int = 0           # 0-100; low -> needs_manual_fix


_VERIFICATION_SYSTEM_PROMPT = """You are verifying a cashback receipt for Cirqle, a UK cashback platform.
You will be shown two images: (1) a REFERENCE receipt previously provided by the merchant,
and (2) a NEW receipt just uploaded by a member claiming cashback.

Assess two independent things:

1. AUTHENTICITY OF ORIGIN — does the NEW receipt plausibly come from the same store/vendor
   as the REFERENCE receipt? Compare store name, address, phone, VAT number if visible, and
   overall visual format (font, layout, header/footer structure, logo). Minor differences
   (different till number, different date/time, different items) are EXPECTED and not a
   mismatch — you are checking vendor identity, not that the two receipts are identical.
   A chain may print receipts slightly differently across locations — weight matched fields
   (name/address/phone) over pure visual similarity, and score conservatively on ambiguity
   rather than confidently either way.

2. PURCHASE CORRECTNESS — does the NEW receipt plausibly show a purchase of the campaign
   product described below, for a sane amount, on a sane date? Flag if the total is
   implausibly low/high for the product, if the date is missing or clearly invalid, or if
   line items clearly don't relate to the campaign product — but do NOT hard-fail on minor
   ambiguity (e.g. a generic line-item description); use "uncertain" rather than "no" in that
   case and explain why in reasoning.

Be conservative: your scores can directly auto-approve a cash payout with no human review
when high enough, so if you are unsure, score lower rather than higher. Reserve scores above
0.9 for cases with genuinely strong evidence on both dimensions.

Also extract the NEW receipt's fields for the record.

Return ONLY valid JSON matching this exact schema, no other text:

{
  "ocr_fields": {
    "store_name": string | null,
    "date": string | null,
    "total": number | null,
    "currency": string | null,
    "line_items": [string]
  },
  "authenticity": {
    "score": number,
    "matched_fields": [string],
    "mismatches": [string],
    "reasoning": string
  },
  "purchase_match": {
    "score": number,
    "product_match": "yes" | "no" | "uncertain",
    "amount_plausible": boolean,
    "date_plausible": boolean,
    "reasoning": string
  },
  "red_flags": [string]
}
"""

_REFERENCE_SYSTEM_PROMPT = """You are reading a merchant's reference receipt for Cirqle, a UK cashback
platform. This single image will be compared against every future receipt a shopper submits
for this merchant's deals, so extract what makes this vendor's receipts recognisable.

Return ONLY valid JSON matching this exact schema, no other text:

{
  "store_name": string,
  "address": string,
  "phone": string,
  "vat_number": string,
  "format_notes": string,   // 1-3 sentences describing the visual layout: font, header/footer
                             // structure, logo placement — whatever would help spot a receipt
                             // that does NOT come from the same till/printer system.
  "confidence": number      // 0-100, how legible and complete this reference is
}
"""


def _clean_json(text: str) -> dict:
    cleaned = text.replace("```json", "").replace("```", "").strip()
    return json.loads(cleaned)


def _b64(image_bytes: bytes) -> str:
    import base64
    return base64.b64encode(image_bytes).decode("ascii")


def _client():
    if not ANTHROPIC_API_KEY:
        raise VerificationCallError("ANTHROPIC_API_KEY is not set.")
    import anthropic  # lazy, matching boto3's lazy-import convention elsewhere
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def extract_reference_fields(image_bytes: bytes) -> ReferenceExtraction:
    """One synchronous Claude call reading a merchant's reference receipt.
    Raises VerificationCallError on any failure — no retry (rare, merchant-
    triggered action; a failure just surfaces as an error to retry by hand)."""
    try:
        response = _client().messages.create(
            model=VERIFICATION_MODEL,
            max_tokens=500,
            system=_REFERENCE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": _b64(image_bytes)}},
            ]}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        return ReferenceExtraction.model_validate(_clean_json(text))
    except VerificationCallError:
        raise
    except Exception as exc:  # noqa: BLE001 — any failure here is one type to the caller
        raise VerificationCallError(f"Reference extraction failed: {exc}") from exc


def call_claude_verification(reference_bytes: bytes, new_bytes: bytes,
                             campaign_context: str) -> ClaudeReceiptVerification:
    """One multimodal Claude call, one retry on transient failure. Raises
    VerificationCallError if both attempts fail — never returns a value the
    caller could mistake for a valid result."""
    content = [
        {"type": "text", "text": campaign_context},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": _b64(reference_bytes)}},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": _b64(new_bytes)}},
    ]
    last_error: Optional[Exception] = None
    for attempt in range(2):
        try:
            response = _client().messages.create(
                model=VERIFICATION_MODEL,
                max_tokens=1000,
                system=_VERIFICATION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            )
            text = "".join(block.text for block in response.content if block.type == "text")
            return ClaudeReceiptVerification.model_validate(_clean_json(text))
        except VerificationCallError:
            raise
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
        except Exception as exc:  # noqa: BLE001 — network/5xx/etc, one retry
            last_error = exc
        if attempt == 0:
            time.sleep(1)
    raise VerificationCallError(f"Claude verification failed after retry: {last_error}")


def image_phash(image_bytes: bytes) -> str:
    """Perceptual hash (pHash), hex string. Lazy imports (PIL/imagehash),
    matching the boto3 lazy-import convention used throughout storage.py."""
    from PIL import Image
    import imagehash
    return str(imagehash.phash(Image.open(io.BytesIO(image_bytes))))


def _hamming(a: str, b: str) -> int:
    import imagehash
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


def find_duplicate_hash(session: Session, new_hash: str, exclude_receipt_id: int) -> Optional[Receipt]:
    """First prior receipt whose image hash is within IMAGE_HASH_THRESHOLD of
    new_hash, or None. O(n) over receipts with a stored hash — fine at
    current volume; revisit (e.g. hash-prefix bucketing) if it grows."""
    candidates = session.exec(
        select(Receipt).where(Receipt.image_hash.is_not(None), Receipt.image_hash != "",
                              Receipt.id != exclude_receipt_id)
    ).all()
    for candidate in candidates:
        if _hamming(new_hash, candidate.image_hash) <= IMAGE_HASH_THRESHOLD:
            return candidate
    return None


def build_campaign_context(campaign: Optional[Campaign]) -> str:
    """Plain-text campaign context for the prompt. Handles campaign is None
    (a visit claim or an unlinked receipt has no campaign to check against)."""
    if campaign is None:
        return "CAMPAIGN CONTEXT: no specific deal is linked to this claim."
    lines = [
        "CAMPAIGN CONTEXT:",
        f"Brand: {campaign.brand}",
        f"Product/deal: {campaign.card_title or campaign.title}",
    ]
    if campaign.card_desc:
        lines.append(f"Description: {campaign.card_desc}")
    required = _required_spend(campaign)
    if required is not None:
        lines.append(f"Must show a spend of at least £{required:.2f}.")
    return "\n".join(lines)


def decide_verification_status(auto_approval_enabled: bool, duplicate_hash_hit: bool,
                               overall_score: Optional[float], threshold: float) -> str:
    """The pure HITL branch function — table-tested directly, no I/O. Exact
    order matters: the kill switch and the duplicate-hash override both take
    priority over the score, and a missing score never auto-approves."""
    if not auto_approval_enabled:
        return "pending_admin_review"
    if duplicate_hash_hit:
        return "pending_admin_review"
    if overall_score is not None and overall_score >= threshold:
        return "auto_approved"
    return "pending_admin_review"


def trigger_cashback_calculation(receipt_id: int) -> None:
    """STUB — the real payout algorithm is a separate future item (currently
    disabled, being redesigned — see routers/receipts.py:_apply_shadow_scoring).
    Safe to call from either the auto-approval path or the admin-approval
    path; guards against double-firing since both now exist. Opens its own
    session at call time (see module docstring on db.engine timing) so either
    caller can use it identically without handing it a session.
    """
    with Session(db.engine) as session:
        receipt = session.get(Receipt, receipt_id)
        if receipt is None:
            return
        if receipt.cashback_triggered_at is not None:
            print(f"cashback already triggered for receipt {receipt_id}, skipping")
            return
        receipt.cashback_triggered_at = datetime.utcnow()
        print(f"cashback triggered for receipt {receipt_id}, amount={receipt.amount}, "
              f"source={receipt.decision_source}")
        session.add(receipt)
        session.commit()


def run_claude_verification(receipt_id: int) -> None:
    """The background-task entry point, run after every receipt upload.

    Opens its own session at CALL time via db.engine (not `from .db import
    engine` at import time) — pytest imports every test module during
    collection, before any fixture runs, so an import-time binding would
    silently land on the real database in tests. This is the exact bug class
    app/verify.py was fixed for earlier; do not reintroduce it here.
    """
    with Session(db.engine) as session:
        receipt = session.get(Receipt, receipt_id)
        if receipt is None:
            return

        campaign = session.get(Campaign, receipt.campaign_id) if receipt.campaign_id else None
        merchant = session.get(Merchant, campaign.merchant_id) if campaign and campaign.merchant_id else None

        if merchant is None or merchant.reference_status != "ready":
            receipt.verification_status = "pending_admin_review"
            receipt.verification_error = "merchant reference receipt not ready"
            session.add(receipt)
            session.commit()
            return

        try:
            image_bytes = read_receipt(receipt.image_key)
            if not image_bytes:
                raise VerificationCallError("the stored receipt image could not be read")
            reference_bytes = read_receipt(merchant.reference_receipt_s3_key)
            if not reference_bytes:
                raise VerificationCallError("the merchant's reference receipt image could not be read")

            new_hash = image_phash(image_bytes)
            receipt.image_hash = new_hash
            duplicate = find_duplicate_hash(session, new_hash, receipt.id)

            result = call_claude_verification(
                reference_bytes, image_bytes, build_campaign_context(campaign))

            receipt.ocr_fields = json.dumps(result.ocr_fields.model_dump())
            receipt.authenticity_score = result.authenticity.score
            auth_detail = result.authenticity.model_dump(exclude={"score"})
            auth_detail["red_flags"] = result.red_flags
            if duplicate is not None:
                auth_detail["duplicate_hash_match_receipt_id"] = duplicate.id
            receipt.authenticity_detail = json.dumps(auth_detail)
            receipt.purchase_match_score = result.purchase_match.score
            receipt.purchase_match_detail = json.dumps(result.purchase_match.model_dump(exclude={"score"}))
            overall = 0.5 * result.authenticity.score + 0.5 * result.purchase_match.score
            receipt.overall_score = overall

            # Keeps merchant revenue analytics (customers.py, merchant.py)
            # working — those columns are read independent of verification
            # outcome, so they're populated on every successful call.
            apply_reading(receipt, result.ocr_fields.model_dump())

            status = decide_verification_status(
                AUTO_APPROVAL_ENABLED, duplicate is not None, overall,
                VERIFICATION_AUTO_APPROVE_THRESHOLD)
            receipt.verification_status = status
            if status == "auto_approved":
                receipt.decision_source = "auto"
                receipt.decision_at = datetime.utcnow()
                # Critical: verification_status alone does not gate cashback —
                # status is what cashback.py's clears_at/effective_status
                # actually reads. Without this, an "auto_approved" claim would
                # silently expire at the 3-day mark, never having verified.
                receipt.status = "verified"
        except Exception as exc:  # noqa: BLE001 — a failure must never auto-approve
            receipt.verification_status = "pending_admin_review"
            receipt.verification_error = str(exc)[:500]

        session.add(receipt)
        session.commit()
        auto_approved = receipt.verification_status == "auto_approved"

    if auto_approved:
        trigger_cashback_calculation(receipt_id)
