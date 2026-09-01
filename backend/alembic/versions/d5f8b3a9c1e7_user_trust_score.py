"""user trust score: per-user Account Quality Score cache

Revision ID: d5f8b3a9c1e7
Revises: c1a9e4f7b2d6
Create Date: 2026-09-01

Phase 2 of the performance-cashback rollout. Adds `usertrustscore`, a
write-through cache/audit table for the AQS computed by app/aqs.py — one row
per user, upserted by backend/scripts/backfill_aqs_scores.py and (in a later
phase) at claim-approval time. Not the source of truth: AQS is always
recomputed live from Mention data wherever it's needed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd5f8b3a9c1e7'
down_revision: Union[str, Sequence[str], None] = 'c1a9e4f7b2d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'usertrustscore',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('aqs_score', sa.Float(), nullable=False),
        sa.Column('aqs_computed_at', sa.DateTime(), nullable=False),
        sa.Column('aqs_inputs_snapshot', sa.Text(), nullable=False, server_default='{}'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_usertrustscore_user_id'), 'usertrustscore', ['user_id'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_usertrustscore_user_id'), table_name='usertrustscore')
    op.drop_table('usertrustscore')
