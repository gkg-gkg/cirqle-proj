"""add merchant membership billing: Stripe customer, tier, subscription state

Revision ID: b7d4c9e15f80
Revises: e2b9d47c1a05
Create Date: 2026-08-31

Adds the columns behind merchant membership plans:
  • merchant            — Stripe customer id, tier, subscription id/status/renewal
  • merchanttransaction — `fee` (the 10% charged past the monthly allowance)
  • merchantapplication — `tier` (plan asked for) + `kind` (application/enquiry)

Existing merchants are GRANDFATHERED onto an active plan (see _GRANDFATHER_TIER).
They pre-date plans entirely, and without this the deploy would instantly lock
them out of top-ups and new deal submissions until they subscribed — with no way
to subscribe until Stripe keys are live on the server. They get no Stripe
subscription, so they are never charged; if they later choose a plan in the
portal, Checkout replaces this state properly. No balance is touched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'b7d4c9e15f80'
down_revision: Union[str, Sequence[str], None] = 'e2b9d47c1a05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STR = sqlmodel.sql.sqltypes.AutoString

# Which plan existing merchants are placed on. Middle tier: a £500/month
# fee-free allowance, so a long-standing partner isn't suddenly hit with
# platform fees on their normal top-ups.
_GRANDFATHER_TIER = "growth"

# table -> [(column, type, server_default)]
_ADDITIONS = {
    'merchant': [
        ('stripe_customer_id', _STR(), ''),
        ('tier', _STR(), ''),
        ('stripe_subscription_id', _STR(), ''),
        ('subscription_status', _STR(), 'none'),
    ],
    'merchanttransaction': [
        ('fee', sa.Float(), '0'),
    ],
    'merchantapplication': [
        ('tier', _STR(), ''),
        ('kind', _STR(), 'application'),
    ],
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, columns in _ADDITIONS.items():
        existing = {c['name'] for c in inspector.get_columns(table)}
        for name, type_, default in columns:
            if name not in existing:
                op.add_column(table, sa.Column(name, type_, nullable=False,
                                               server_default=default))
    # Nullable — a merchant without a plan has no renewal date.
    if 'current_period_end' not in {c['name'] for c in inspector.get_columns('merchant')}:
        op.add_column('merchant', sa.Column('current_period_end', sa.DateTime(),
                                            nullable=True))
    op.create_index('ix_merchant_stripe_customer_id', 'merchant', ['stripe_customer_id'])

    # Grandfather every pre-existing merchant. Any row present now pre-dates
    # plans by definition, so this can't affect a genuine subscriber.
    op.execute(
        f"UPDATE merchant SET tier = '{_GRANDFATHER_TIER}', subscription_status = 'active' "
        "WHERE tier = '' OR tier IS NULL")


def downgrade() -> None:
    op.drop_index('ix_merchant_stripe_customer_id', table_name='merchant')
    op.drop_column('merchant', 'current_period_end')
    for table, columns in _ADDITIONS.items():
        for name, _type, _default in columns:
            op.drop_column(table, name)
