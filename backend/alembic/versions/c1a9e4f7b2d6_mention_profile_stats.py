"""mention profile stats: follower/following counts at scrape time

Revision ID: c1a9e4f7b2d6
Revises: b7d4c9e15f80
Create Date: 2026-09-01

Phase 1 of the performance-cashback rollout (see cashback-algorithm spec):
instrumentation only, zero behavior change. Adds two nullable columns to
`mention` so the account-quality scoring in a later phase has real
follower/following data to work with, instead of always falling back to a
neutral default. Nothing reads these columns yet.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c1a9e4f7b2d6'
down_revision: Union[str, Sequence[str], None] = 'b7d4c9e15f80'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('mention', schema=None) as batch_op:
        batch_op.add_column(sa.Column('follower_count_at_scrape', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('following_count_at_scrape', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('mention', schema=None) as batch_op:
        batch_op.drop_column('following_count_at_scrape')
        batch_op.drop_column('follower_count_at_scrape')
