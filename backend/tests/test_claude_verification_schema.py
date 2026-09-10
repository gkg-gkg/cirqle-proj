"""Pydantic validation of Claude's JSON contract, and call_claude_verification's
retry-then-raise behavior on repeated failure (Anthropic client always mocked
-- no real network call)."""
import json

import pytest
from pydantic import ValidationError

from app.receipt_verification import (ClaudeReceiptVerification,
                                       VerificationCallError,
                                       call_claude_verification)

VALID_SAMPLE = {
    "ocr_fields": {"store_name": "TESCO", "date": "2026-09-01", "total": 42.1,
                   "currency": "GBP", "line_items": ["Milk", "Bread"]},
    "authenticity": {"score": 0.9, "matched_fields": ["store_name"], "mismatches": [],
                     "reasoning": "Matches."},
    "purchase_match": {"score": 0.8, "product_match": "yes", "amount_plausible": True,
                       "date_plausible": True, "reasoning": "Plausible."},
    "red_flags": [],
}


def test_valid_sample_parses():
    result = ClaudeReceiptVerification.model_validate(VALID_SAMPLE)
    assert result.authenticity.score == 0.9
    assert result.purchase_match.product_match == "yes"


def test_missing_authenticity_block_raises():
    bad = {k: v for k, v in VALID_SAMPLE.items() if k != "authenticity"}
    with pytest.raises(ValidationError):
        ClaudeReceiptVerification.model_validate(bad)


def test_score_out_of_range_raises():
    bad = json.loads(json.dumps(VALID_SAMPLE))
    bad["authenticity"]["score"] = 1.5
    with pytest.raises(ValidationError):
        ClaudeReceiptVerification.model_validate(bad)


def test_wrong_type_for_product_match_raises():
    bad = json.loads(json.dumps(VALID_SAMPLE))
    bad["purchase_match"]["product_match"] = True   # should be a string
    with pytest.raises(ValidationError):
        ClaudeReceiptVerification.model_validate(bad)


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, texts):
        self._texts = list(texts)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        text = self._texts[min(self.calls - 1, len(self._texts) - 1)]
        return _FakeResponse(text)


class _FakeClient:
    def __init__(self, texts):
        self.messages = _FakeMessages(texts)


def test_call_claude_verification_succeeds_first_try(monkeypatch):
    fake = _FakeClient([json.dumps(VALID_SAMPLE)])
    monkeypatch.setattr("app.receipt_verification._client", lambda: fake)
    result = call_claude_verification(b"ref", b"new", "context")
    assert result.authenticity.score == 0.9
    assert fake.messages.calls == 1


def test_call_claude_verification_retries_once_then_succeeds(monkeypatch):
    fake = _FakeClient(["not json", json.dumps(VALID_SAMPLE)])
    monkeypatch.setattr("app.receipt_verification._client", lambda: fake)
    monkeypatch.setattr("app.receipt_verification.time.sleep", lambda *_: None)
    result = call_claude_verification(b"ref", b"new", "context")
    assert result.authenticity.score == 0.9
    assert fake.messages.calls == 2


def test_call_claude_verification_raises_after_both_attempts_fail(monkeypatch):
    fake = _FakeClient(["not json", "still not json"])
    monkeypatch.setattr("app.receipt_verification._client", lambda: fake)
    monkeypatch.setattr("app.receipt_verification.time.sleep", lambda *_: None)
    with pytest.raises(VerificationCallError):
        call_claude_verification(b"ref", b"new", "context")
    assert fake.messages.calls == 2


def test_call_claude_verification_missing_api_key_raises(monkeypatch):
    monkeypatch.setattr("app.receipt_verification.ANTHROPIC_API_KEY", "")
    with pytest.raises(VerificationCallError):
        call_claude_verification(b"ref", b"new", "context")
