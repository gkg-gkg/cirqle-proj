"""visit claims: a receipt with no Instagram post behind it

Revision ID: e83c26d1f409
Revises: d41f7b0c8e35
Create Date: 2026-09-08

A claim used to require an Instagram post, and was unique on (user_id, post_id).
That meant a customer who came back four times without posting was recorded
once, and every retention figure the merchant dashboard reports was really
measuring repeat POSTING rather than repeat visiting.

A visit claim is the receipt on its own. It earns whatever the merchant chose
for that campaign, and counts fully towards their customer and retention
numbers.

Existing rows are all 'post' claims, which is what they were. Both new campaign
columns default to off, matching how referral bonuses work — funding a wallet
must never enrol a deal the brand didn't choose.

post_id keeps its NOT NULL and stores '' for a visit claim rather than becoming
nullable: '' is a value the (user_id, post_id) lookup in routers/receipts.py can
be guarded against, and NULL comparison semantics differ between SQLite and
Postgres in exactly the place that lookup would break.
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'e83c26d1f409'
down_revision: Union[str, Sequence[str], None] = 'd41f7b0c8e35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('receipt', sa.Column(
        'claim_kind', sqlmodel.sql.sqltypes.AutoString(),
        nullable=False, server_default='post'))
    op.create_index('ix_receipt_claim_kind', 'receipt', ['claim_kind'])

    op.add_column('campaign', sa.Column(
        'visits_enabled', sa.Boolean(), nullable=False,
        server_default=sa.false()))
    op.add_column('campaign', sa.Column(
        'visit_earn', sa.Float(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('campaign', 'visit_earn')
    op.drop_column('campaign', 'visits_enabled')
    op.drop_index('ix_receipt_claim_kind', table_name='receipt')
    op.drop_column('receipt', 'claim_kind')
