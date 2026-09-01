"""performance cashback payout: campaign budget/mode + receipt audit fields

Revision ID: a6c3f9d1b8e4
Revises: d5f8b3a9c1e7
Create Date: 2026-09-01

Phase 3 of the performance-cashback rollout: shadow-mode payout logging.
`campaign.cashback_mode` defaults to 'flat' for every existing and new row,
so the new budget/multiplier columns are inert until an admin explicitly
opts a campaign into 'performance' mode (not done by this migration or any
code in this build) — no existing payout behavior changes.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a6c3f9d1b8e4'
down_revision: Union[str, Sequence[str], None] = 'd5f8b3a9c1e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('campaign', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cashback_mode', sa.String(), nullable=False, server_default='flat'))
        batch_op.add_column(sa.Column('base_cashback', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('expected_engagement_baseline', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('max_multiplier', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('per_post_cap', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('budget_total', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('budget_remaining', sa.Float(), nullable=True))

    with op.batch_alter_table('receipt', schema=None) as batch_op:
        batch_op.add_column(sa.Column('aqs_score_at_approval', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('engagement_multiplier_at_approval', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('engagement_snapshot', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('algorithm_version', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('shadow_payout', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('receipt', schema=None) as batch_op:
        batch_op.drop_column('shadow_payout')
        batch_op.drop_column('algorithm_version')
        batch_op.drop_column('engagement_snapshot')
        batch_op.drop_column('engagement_multiplier_at_approval')
        batch_op.drop_column('aqs_score_at_approval')

    with op.batch_alter_table('campaign', schema=None) as batch_op:
        batch_op.drop_column('budget_remaining')
        batch_op.drop_column('budget_total')
        batch_op.drop_column('per_post_cap')
        batch_op.drop_column('max_multiplier')
        batch_op.drop_column('expected_engagement_baseline')
        batch_op.drop_column('base_cashback')
        batch_op.drop_column('cashback_mode')
