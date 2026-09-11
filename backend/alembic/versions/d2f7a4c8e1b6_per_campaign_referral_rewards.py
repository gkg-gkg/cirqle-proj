"""per-campaign referral reward amounts

Revision ID: d2f7a4c8e1b6
Revises: c7e9a2f5b1d4
Create Date: 2026-09-11

Referral bonuses used to be one flat pair of amounts for the whole site —
REWARD_REFERRER (£1) and REWARD_REFEREE (50p) in app/referrals.py — the same
for a £2 coffee deal as for a £2,000 holiday. Merchants can now set their own
pair per campaign, with a £1 floor enforced by the merchant endpoint
(routers/merchant.py's set_deal_referrals), not by the database.

Existing rows get the old constants as their starting values (1.0 / 0.5) so
every already-configured deal keeps paying exactly what it always has until a
merchant deliberately changes it — this migration doesn't reprice anything on
its own. Note the referee default (0.5) sits below the new £1 floor: that
floor only binds a merchant's own edit going forward, not what's already
there.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd2f7a4c8e1b6'
down_revision: Union[str, Sequence[str], None] = 'c7e9a2f5b1d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('campaign', sa.Column(
        'referral_reward_referrer', sa.Float(), nullable=False,
        server_default='1.0'))
    op.add_column('campaign', sa.Column(
        'referral_reward_referee', sa.Float(), nullable=False,
        server_default='0.5'))


def downgrade() -> None:
    # Batched for the same reason as e83c26d1f409: SQLite below 3.35 can't
    # DROP COLUMN outright, and the local runtime is 3.34.
    with op.batch_alter_table('campaign') as batch:
        batch.drop_column('referral_reward_referee')
        batch.drop_column('referral_reward_referrer')
