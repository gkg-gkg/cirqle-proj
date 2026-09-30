"""merchant.trial_used + merchant.trialing: the free first month

Revision ID: a4e8b2c60f19
Revises: c9f2e6a4d1b3
Create Date: 2026-09-29

Every membership tier now starts with a free month, applied as
trial_period_days on the Stripe Checkout session.

`trial_used` — Stripe will happily grant a trial on every new subscription,
so without a flag of our own a brand could cancel and resubscribe for a free
month indefinitely. Existing merchants default to False, which means anyone
already subscribed gets one free month the next time they start a
subscription. That is the kinder of the two defaults, and the population is
small enough that backfilling True for brands who have already paid us is not
worth the risk of getting it wrong.

`trialing` — subscription_status maps Stripe's "trialing" onto "active",
because every permission question treats them alike, and changing that would
mean auditing every access check. This carries the distinction separately so
the portal can say "first charge" rather than "renews". Presentational only.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a4e8b2c60f19'
down_revision: Union[str, Sequence[str], None] = 'c9f2e6a4d1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('merchant')}
    for name in ('trial_used', 'trialing'):
        if name not in existing:
            op.add_column('merchant', sa.Column(name, sa.Boolean(), nullable=False,
                                                server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('merchant', 'trialing')
    op.drop_column('merchant', 'trial_used')
