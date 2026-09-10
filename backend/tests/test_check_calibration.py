"""The calibration report: does the check's score match the admin's decision?

This is the evidence Phase 3 needs before auto-approval can be switched on.
"""
import pytest
from fastapi import BackgroundTasks, HTTPException

from app.models import Receipt, User
from app.routers.receipts import check_calibration, recheck_receipt


@pytest.fixture
def claims(session):
    session.add(User(id=1, email="a@x.com", first_name="A", last_name="A",
                     password_hash="x"))
    session.commit()

    def _seed(rows):
        """rows = [(claim status, check status, score)]"""
        for i, (status, check_status, score) in enumerate(rows, start=1):
            session.add(Receipt(id=i, user_id=1, post_id=f"p{i}", image_key=f"k{i}",
                                status=status, check_status=check_status,
                                check_score=score))
        session.commit()
        return check_calibration(session=session)
    return _seed


def test_no_data_asks_for_data(claims):
    report = claims([])
    assert report.decided == 0
    assert report.verdict.startswith("No decided claims")


def test_approvals_without_rejections_cannot_calibrate(claims):
    """With nothing rejected there is no evidence of what a bad claim scores."""
    report = claims([("confirmed", "ok", 95), ("paid", "ok", 88)])
    assert report.approved == 2
    assert report.suggestedThreshold is None
    assert "needs rejections" in report.verdict


def test_clean_separation_suggests_a_threshold(claims):
    report = claims([("confirmed", "ok", 95), ("confirmed", "ok", 92),
                     ("paid", "ok", 88), ("verified", "ok", 85),
                     ("rejected", "ok", 40), ("rejected", "ok", 25)])
    assert (report.approved, report.rejected) == (4, 2)
    # The threshold must clear the best score a human still rejected.
    assert report.highestRejectedScore == 40
    assert report.suggestedThreshold == 41
    assert report.wouldAutoApprove == 4
    assert report.coveragePct == 100
    assert "without touching anything you rejected" in report.verdict


def test_a_rejection_scoring_full_marks_means_no_safe_threshold(claims):
    report = claims([("confirmed", "ok", 95), ("rejected", "ok", 100)])
    assert report.suggestedThreshold is None
    assert "no threshold is safe" in report.verdict


def test_poor_separation_is_called_out(claims):
    report = claims([("confirmed", "ok", 95)] + [("confirmed", "ok", 70)] * 9
                    + [("rejected", "ok", 90)])
    assert report.suggestedThreshold == 91
    assert (report.wouldAutoApprove, report.coveragePct) == (1, 10)
    assert "barely worth" in report.verdict


def test_unchecked_claims_are_excluded_not_assumed_good(claims):
    report = claims([("confirmed", "ok", 95), ("confirmed", "", 0),
                     ("rejected", "error", 0), ("pending", "ok", 99),
                     ("rejected", "ok", 30)])
    assert report.decided == 2          # only the two that were actually checked
    assert report.unchecked == 2
    assert report.approved + report.rejected == 2   # 'pending' is not a decision


def test_buckets(claims):
    report = claims([("confirmed", "ok", 95), ("confirmed", "ok", 91),
                     ("confirmed", "ok", 85), ("rejected", "ok", 5)])
    band = {b.label: (b.approved, b.rejected) for b in report.buckets}
    assert band["90-99"] == (2, 0)
    assert band["80-89"] == (1, 0)
    assert band["0-9"] == (0, 1)
    assert len(report.buckets) == 10


class TestRecheck:
    """Checks fail for ordinary reasons; there must be a way to retry one."""

    def test_schedules_a_fresh_check(self, session, claims):
        claims([("pending", "error", 0)])
        background = BackgroundTasks()
        out = recheck_receipt(receipt_id=1, background=background, session=session)
        assert len(background.tasks) == 1
        assert out.id == 1

    def test_unknown_claim_is_a_404(self, session, claims):
        claims([])
        with pytest.raises(HTTPException) as raised:
            recheck_receipt(receipt_id=999, background=BackgroundTasks(),
                            session=session)
        assert raised.value.status_code == 404
