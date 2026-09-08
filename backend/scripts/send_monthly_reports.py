"""Email every active merchant their monthly performance summary.

The highest-leverage thing in the analytics work, because it is the only one
that reaches a brand who never logs in — and a brand that can't see what its
cashback bought doesn't re-fund the wallet.

Run it from cron on the first of the month:

    0 9 1 * *  cd /path/to/backend && .venv/bin/python scripts/send_monthly_reports.py

There is no scheduler inside the app, deliberately — cashback and referral
bonuses settle on read for the same reason (see app/referrals.py). This stays a
script so the only thing that ever needs a timer lives outside the request path.

    python scripts/send_monthly_reports.py --dry-run          # print, send nothing
    python scripts/send_monthly_reports.py --merchant 4       # just one, for testing

Merchants with no activity at all are skipped rather than sent a page of
zeroes, which reads as a dead product rather than a quiet month.
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select                            # noqa: E402

from app import mailer                                          # noqa: E402
from app.customers import (build_customers, incrementality,      # noqa: E402
                           repeat_rate, summary)
from app.db import engine                                       # noqa: E402
from app.models import Merchant                                 # noqa: E402
from app.routers.merchant import _compute_stats                 # noqa: E402


def _money(value: float) -> str:
    return f"£{value:,.2f}"


def _period(today: date) -> str:
    """The month just gone, which is what the report covers."""
    first = today.replace(day=1)
    last_month = first.replace(year=first.year - 1, month=12) if first.month == 1 \
        else first.replace(month=first.month - 1)
    return last_month.strftime("%B %Y")


def build_report(merchant: Merchant, session: Session, today: date):
    """(headline, rows) for one merchant, or None if there's nothing to say."""
    stats = _compute_stats(merchant, session)
    records = build_customers(merchant.id, session)
    if not records and not stats.claims:
        return None

    figures = summary(records, session, merchant.id, today)
    fresh = incrementality(records, stats.revenue, stats.programmeCost, today)

    if stats.revenue and stats.programmeCost:
        headline = (f"Your deals brought in {_money(stats.revenue)} of tracked "
                    f"sales for {_money(stats.programmeCost)} of cashback — "
                    f"{stats.returnOnCashback:.2f}× back on what you spent.")
    elif stats.claims:
        headline = (f"{stats.claims} claims this period. We couldn't read a "
                    f"total off enough receipts to report sales yet.")
    else:
        headline = "A quiet period — no claims yet."

    rows = [
        ("Tracked sales", _money(stats.revenue),
         f"read from {stats.revenueCoverage}% of approved claims"),
        ("What it cost you", _money(stats.programmeCost),
         "cashback, referral bonuses and fees"),
        ("Return", f"{stats.returnOnCashback:.2f}×" if stats.returnOnCashback
         else "—", "sales per £1 spent"),
        ("Customers", str(figures["customers"]),
         f"{fresh['newCustomers']} new to you on Cirqle"),
        ("Came back", f"{figures['repeatRate']}%",
         "of customers who've had the chance to"),
        ("Referred customers", str(figures["referredCustomers"]),
         "arrived through someone else's post"),
    ]
    return headline, rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print each report instead of emailing it")
    ap.add_argument("--merchant", type=int,
                    help="only this merchant id, for testing")
    args = ap.parse_args()

    today = date.today()
    period = _period(today)
    sent = skipped = failed = 0

    with Session(engine) as session:
        query = select(Merchant)
        if args.merchant:
            query = query.where(Merchant.id == args.merchant)
        for merchant in session.exec(query).all():
            # An unverified address can't receive anything, and a merchant who
            # never set a password hasn't onboarded yet.
            if merchant.email_verified_at is None or merchant.must_set_password:
                skipped += 1
                continue

            report = build_report(merchant, session, today)
            if report is None:
                skipped += 1
                continue
            headline, rows = report

            if args.dry_run:
                print(f"\n── {merchant.business_name} <{merchant.email}> — {period}")
                print(f"   {headline}")
                for label, value, note in rows:
                    print(f"     {label:<20} {value:>12}   {note}")
                sent += 1
                continue

            if mailer.send_merchant_report(merchant.email, merchant.business_name,
                                           period, rows, headline):
                sent += 1
            else:
                failed += 1

    verb = "would send" if args.dry_run else "sent"
    print(f"\n{period}: {verb} {sent} · skipped {skipped} · failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
