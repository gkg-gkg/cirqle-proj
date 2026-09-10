"""Scoring a reading against the deal, and the background check that stores it."""
import json
from datetime import datetime

import pytest

from app import verify
from app.models import Campaign, Mention, Receipt, User
from app.verify import (ReceiptReading, _merchant_matches, _required_spend,
                        _score, check_receipt, summarise)

POST_TS = datetime(2026, 8, 30, 10, 0)


def reading(**overrides):
    base = dict(is_receipt=True, document_type="receipt",
                merchant_name="TESCO STORES LTD", total=42.10, currency="GBP",
                purchase_date="2026-08-28", order_number="48213",
                totals_add_up=True, vat_consistent=True, confidence=95,
                concerns=[])
    base.update(overrides)
    return ReceiptReading(**base)


@pytest.mark.parametrize("printed,brand,expected", [
    ("BOOTS UK LTD", "Boots", True),
    ("TESCO STORES LIMITED", "Tesco", True),
    ("BOOTS UK LTD", "Tesco", False),
    ("The Ltd", "Tesco", False),        # only noise words — no real match
])
def test_merchant_matching(printed, brand, expected):
    assert _merchant_matches(printed, brand) is expected


@pytest.mark.parametrize("spend_desc,expected", [
    ("on a £100 spend", 100.0), ("on a £1,250.50 spend", 1250.5), ("", None),
])
def test_required_spend(spend_desc, expected):
    assert _required_spend(Campaign(spend_desc=spend_desc)) == expected


@pytest.fixture
def deal(session):
    session.add(User(id=1, email="a@x.com", first_name="Ann", last_name="A",
                     password_hash="x"))
    session.add(Campaign(id=1, brand="Tesco", earn="£5.00",
                         spend_desc="on a £30 spend"))
    session.add(Mention(id="post1", user_id=1, timestamp="2026-08-30T10:00:00Z"))
    session.commit()
    return session.get(Campaign, 1)


@pytest.fixture
def claim():
    return Receipt(id=999, user_id=1, post_id="post1", campaign_id=1, image_key="k")


def score(session, deal, claim, **overrides):
    return _score(reading(**overrides), claim, deal, POST_TS, session)


class TestScoring:
    def test_clean_receipt(self, session, deal, claim):
        assert score(session, deal, claim) == (100, [])

    def test_not_a_receipt_scores_zero(self, session, deal, claim):
        assert score(session, deal, claim, is_receipt=False)[0] == 0

    def test_wrong_merchant(self, session, deal, claim):
        points, why = score(session, deal, claim, merchant_name="BOOTS UK LTD")
        assert points == 60
        assert "BOOTS UK LTD" in why[0] and "Tesco" in why[0]

    def test_totals_do_not_add_up(self, session, deal, claim):
        assert score(session, deal, claim, totals_add_up=False)[0] == 70

    def test_vat_inconsistent(self, session, deal, claim):
        assert score(session, deal, claim, vat_consistent=False)[0] == 80

    def test_unknowable_checks_are_not_penalised(self, session, deal, claim):
        assert score(session, deal, claim,
                     totals_add_up=None, vat_consistent=None)[0] == 100

    def test_under_the_deal_threshold(self, session, deal, claim):
        assert score(session, deal, claim, total=12.00)[0] == 75

    def test_exactly_at_the_threshold_is_fine(self, session, deal, claim):
        assert score(session, deal, claim, total=30.00)[0] == 100

    def test_no_total(self, session, deal, claim):
        assert score(session, deal, claim, total=None)[0] == 90

    def test_bought_after_the_post(self, session, deal, claim):
        assert score(session, deal, claim, purchase_date="2026-09-05")[0] == 70

    def test_bought_long_before_the_post(self, session, deal, claim):
        assert score(session, deal, claim, purchase_date="2026-01-01")[0] == 85

    def test_missing_or_unreadable_date(self, session, deal, claim):
        assert score(session, deal, claim, purchase_date="")[0] == 90
        assert score(session, deal, claim, purchase_date="not-a-date")[0] == 90

    def test_low_confidence(self, session, deal, claim):
        assert score(session, deal, claim, confidence=20)[0] == 85

    def test_concerns_are_listed_but_do_not_deduct(self, session, deal, claim):
        points, why = score(session, deal, claim, concerns=["fonts differ mid-line"])
        assert points == 100
        assert len(why) == 1

    def test_score_clamps_at_zero(self, session, deal, claim):
        points, _ = score(session, deal, claim, merchant_name="BOOTS", total=1.0,
                          totals_add_up=False, purchase_date="2026-09-09",
                          order_number="", confidence=10)
        assert points == 0


class TestDuplicateOrderNumber:
    """The check that image hashing could not do: same purchase, twice."""

    def test_clashing_number_is_caught(self, session, deal, claim):
        session.add(Receipt(id=1, user_id=1, post_id="other", image_key="k2",
                            receipt_number="48213"))
        session.commit()
        points, why = score(session, deal, claim)
        assert points == 50
        assert "#1" in why[0]

    def test_fresh_number_is_fine(self, session, deal, claim):
        session.add(Receipt(id=1, user_id=1, post_id="other", image_key="k2",
                            receipt_number="48213"))
        session.commit()
        assert score(session, deal, claim, order_number="99999")[0] == 100


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Receipts go to a temp dir, and check_receipt opens its own session, so
    it needs pointing at this test's engine too."""
    from app import storage
    path = tmp_path / "receipts"
    path.mkdir()
    monkeypatch.setattr(storage, "RECEIPTS_DIR", path)
    return path


@pytest.fixture
def stored_claim(session, deal, store, monkeypatch):
    monkeypatch.setattr(verify, "engine", session.get_bind())
    (store / "stored.png").write_bytes(b"pretend image")
    claim = Receipt(id=5, user_id=1, post_id="post1", campaign_id=1,
                    image_key="stored.png")
    session.add(claim)
    session.commit()
    return claim


class TestBackgroundCheck:
    def test_records_what_it_read(self, session, stored_claim, monkeypatch):
        monkeypatch.setattr(verify, "_read",
                            lambda data, key: reading(order_number="77777"))
        check_receipt(5)
        session.expire_all()
        stored = session.get(Receipt, 5)
        assert stored.check_status == "ok"
        assert stored.check_score == 100
        assert stored.receipt_number == "77777"
        assert stored.checked_at is not None
        assert json.loads(stored.check_data)["reading"]["merchant_name"] == "TESCO STORES LTD"
        assert summarise(stored)[0] == "TESCO STORES LTD · £42.10 · 2026-08-28 · order 77777"

    def test_extraction_failure_never_escapes(self, session, stored_claim, monkeypatch):
        def boom(data, key):
            raise RuntimeError("Textract is down")
        monkeypatch.setattr(verify, "_read", boom)
        check_receipt(5)                       # must not raise
        session.expire_all()
        stored = session.get(Receipt, 5)
        assert stored.check_status == "error"
        assert json.loads(stored.check_data)["error"] == "Textract is down"
        assert summarise(stored)[0] == "Automated check could not run."

    def test_missing_image_is_an_error_not_a_crash(self, session, stored_claim, monkeypatch):
        monkeypatch.setattr(verify, "_read", lambda data, key: reading())
        stored_claim.image_key = "gone.png"
        session.add(stored_claim)
        session.commit()
        check_receipt(5)
        session.expire_all()
        assert session.get(Receipt, 5).check_status == "error"

    def test_unknown_receipt_is_a_no_op(self, session, monkeypatch):
        monkeypatch.setattr(verify, "engine", session.get_bind())
        check_receipt(123456)                  # must not raise
