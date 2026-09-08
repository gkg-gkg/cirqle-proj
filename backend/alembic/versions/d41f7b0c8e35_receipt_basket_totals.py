"""add receipt basket_total / basket_currency / purchase_date

Revision ID: d41f7b0c8e35
Revises: c8d3f01e59a7
Create Date: 2026-09-08

The automated check has always read these three off the receipt and stored them
inside `check_data`, which is JSON kept as TEXT so the shape is identical on
SQLite and Postgres. That choice is still right, but TEXT cannot be summed or
grouped portably, so the merchant dashboard could never report revenue.

These columns hold the same numbers as real types. Existing rows land NULL / ''
and are filled in by scripts/backfill_basket_totals.py, which re-reads the
`check_data` already on the row — no re-run of Textract, no new API calls.

`basket_total` is the customer's spend. It is NOT `amount` (the flat cashback
the deal pays), and nothing here changes what any member is owed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'd41f7b0c8e35'
down_revision: Union[str, Sequence[str], None] = 'c8d3f01e59a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable on purpose: "no total was legible" is a real, common outcome and
    # must stay distinguishable from a genuine £0.00.
    op.add_column('receipt', sa.Column('basket_total', sa.Float(), nullable=True))
    op.add_column('receipt', sa.Column(
        'basket_currency', sqlmodel.sql.sqltypes.AutoString(),
        nullable=False, server_default=''))
    op.add_column('receipt', sa.Column('purchase_date', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('receipt', 'purchase_date')
    op.drop_column('receipt', 'basket_currency')
    op.drop_column('receipt', 'basket_total')
