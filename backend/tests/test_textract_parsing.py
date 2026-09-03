"""Turning a Textract AnalyzeExpense response into a ReceiptReading."""
import sys

import pytest

from app.verify import (_arithmetic, _currency, _date, _extension,
                        _line_item_prices, _money, _read, _summary)


@pytest.mark.parametrize("text,expected", [
    ("£12.34", 12.34), ("1,234.56", 1234.56), ("$2.50", 2.50),
    ("12", 12.0), ("-4.00", -4.0), ("", None), ("TOTAL", None),
])
def test_money(text, expected):
    assert _money(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("2026-08-28", "2026-08-28"),
    ("28/08/2026", "2026-08-28"),
    # Day-first: on a UK receipt this is 3 August, not 3 March. Reading it the
    # American way would silently corrupt every ambiguous date.
    ("03/08/2026", "2026-08-03"),
    ("28-08-2026", "2026-08-28"),
    ("28 Aug 2026", "2026-08-28"),
    ("Aug 28, 2026", "2026-08-28"),
    ("gibberish", ""),
])
def test_date_is_day_first(text, expected):
    assert _date(text) == expected


def field(kind, value, confidence=99.0, currency=None):
    out = {"Type": {"Text": kind},
           "ValueDetection": {"Text": value, "Confidence": confidence}}
    if currency:
        out["Currency"] = {"Code": currency}
    return out


def document(summary, items):
    return {"SummaryFields": summary,
            "LineItemGroups": [{"LineItems": [
                {"LineItemExpenseFields": [
                    field("ITEM", name), field("PRICE", price),
                ]} for name, price in items]}]}


@pytest.fixture
def tesco():
    return document(
        [field("VENDOR_NAME", "TESCO STORES LTD"),
         field("TOTAL", "£42.10", currency="GBP"),
         field("SUBTOTAL", "£35.08"), field("TAX", "£7.02"),
         field("INVOICE_RECEIPT_DATE", "28/08/2026"),
         field("INVOICE_RECEIPT_ID", "48213")],
        [("MILK", "£12.10"), ("BREAD", "£10.00"), ("COFFEE", "£12.98")])


def test_summary_and_line_items(tesco):
    assert _summary(tesco)["VENDOR_NAME"][0] == "TESCO STORES LTD"
    assert _currency(tesco) == "GBP"
    assert _line_item_prices(tesco) == [12.10, 10.00, 12.98]


def test_highest_confidence_reading_wins():
    fields = _summary({"SummaryFields": [field("TOTAL", "£1.00", 40.0),
                                         field("TOTAL", "£42.10", 95.0)]})
    assert fields["TOTAL"][0] == "£42.10"


class TestArithmetic:
    def test_clean_receipt(self):
        assert _arithmetic(35.08, 7.02, 42.10, [12.10, 10.00, 12.98]) == (True, True, [])

    def test_summary_mismatch_is_reported(self):
        summary_ok, _, why = _arithmetic(35.08, 7.02, 50.00, [12.10, 10.00, 12.98])
        assert summary_ok is False
        assert "42.10" in why[0] and "50.00" in why[0]

    def test_line_items_wildly_off(self):
        _, items_ok, _ = _arithmetic(35.08, 7.02, 42.10, [1.00, 2.00])
        assert items_ok is False

    def test_small_gap_tolerated(self):
        """A voucher or a row Textract missed shouldn't flag an honest receipt."""
        _, items_ok, _ = _arithmetic(35.08, 7.02, 42.10, [12.10, 10.00, 11.00])
        assert items_ok is True

    def test_unknowable_is_none_not_true(self):
        """Saying "fine" about a sum we never did would inflate the score."""
        summary_ok, items_ok, why = _arithmetic(None, None, 42.10, [])
        assert (summary_ok, items_ok) == (None, None)
        assert why == ["No individual item lines could be read."]

    def test_falls_back_to_total_when_no_subtotal(self):
        _, items_ok, _ = _arithmetic(None, None, 42.10, [20.00, 22.10])
        assert items_ok is True


class FakeBoto:
    """Stands in for boto3 so no AWS call is made."""

    def __init__(self, payload):
        self.payload = payload

    def client(self, name, region_name=None):
        return self

    def analyze_expense(self, Document):
        return self.payload


@pytest.fixture
def textract(monkeypatch):
    def _run(payload, image_key="receipt.png"):
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto(payload))
        return _read(b"image-bytes", image_key)
    return _run


def test_read_full_receipt(textract, tesco):
    reading = textract({"ExpenseDocuments": [tesco]})
    assert reading.is_receipt
    assert reading.merchant_name == "TESCO STORES LTD"
    assert reading.total == 42.10
    assert reading.currency == "GBP"
    assert reading.purchase_date == "2026-08-28"
    assert reading.order_number == "48213"
    assert reading.vat_consistent is True
    assert reading.totals_add_up is True
    assert reading.confidence == 99


def test_read_when_nothing_recognised(textract):
    reading = textract({"ExpenseDocuments": []})
    assert reading.is_receipt is False
    assert reading.concerns == ["Textract found no receipt or invoice in this image."]


def test_unparseable_date_is_flagged(textract):
    reading = textract({"ExpenseDocuments": [document(
        [field("VENDOR_NAME", "BOOTS"), field("TOTAL", "£10.00"),
         field("INVOICE_RECEIPT_DATE", "not a date")], [])]})
    assert reading.purchase_date == ""
    assert any("not a date" in c for c in reading.concerns)


def test_format_textract_cannot_read(textract, tesco):
    """Storage accepts .webp; Textract doesn't. Fail readably, not with a stack."""
    with pytest.raises(RuntimeError, match="webp"):
        textract({"ExpenseDocuments": [tesco]}, image_key="receipt.webp")


def test_extension_helper():
    assert _extension("abc123.PNG") == ".png"
    assert _extension("no-extension") == ""
