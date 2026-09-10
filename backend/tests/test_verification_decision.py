"""Table test of decide_verification_status -- the pure HITL branch function.
Every branch, including the exact threshold boundary."""
import pytest

from app.receipt_verification import decide_verification_status

THRESHOLD = 0.85


@pytest.mark.parametrize(
    "auto_approval_enabled,duplicate_hash_hit,overall_score,expected",
    [
        (False, False, 1.0, "pending_admin_review"),      # kill switch off, even at a perfect score
        (False, True, 1.0, "pending_admin_review"),
        (False, False, None, "pending_admin_review"),
        (True, True, 1.0, "pending_admin_review"),          # duplicate overrides a perfect score
        (True, False, 0.85, "auto_approved"),                # exactly at threshold
        (True, False, 0.849999, "pending_admin_review"),      # just under threshold
        (True, False, 0.9, "auto_approved"),
        (True, False, None, "pending_admin_review"),           # no score -> never auto-approve
        (True, False, 0.0, "pending_admin_review"),
    ],
)
def test_decide_verification_status(auto_approval_enabled, duplicate_hash_hit, overall_score, expected):
    result = decide_verification_status(auto_approval_enabled, duplicate_hash_hit, overall_score, THRESHOLD)
    assert result == expected


def test_only_the_auto_approved_branch_is_reachable_with_disabled_kill_switch():
    # regardless of score or duplicate flag, disabled always wins
    for score in (0.0, 0.5, 0.85, 1.0, None):
        for dup in (True, False):
            assert decide_verification_status(False, dup, score, THRESHOLD) == "pending_admin_review"
