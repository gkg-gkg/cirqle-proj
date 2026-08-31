"""add merchanttransaction.stripe_ref for Stripe top-up idempotency

Revision ID: e2b9d47c1a05
Revises: c4f81b2ad930
Create Date: 2026-08-31

`merchanttransaction` predates the migration chain — it was only ever created by
create_all / the merchant-profile script, never a migration. So this migration
is drift-aware:
  • production (table exists, no stripe_ref) -> add the column + its index.
  • a migration-only database (no table)     -> create the table from the model.
Either way it ends with a `merchanttransaction` table that has `stripe_ref`.
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlmodel import SQLModel

import app.models  # noqa: F401 — registers every table on SQLModel.metadata

revision: str = 'e2b9d47c1a05'
down_revision: Union[str, Sequence[str], None] = 'c4f81b2ad930'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'merchanttransaction'
_INDEX = 'ix_merchanttransaction_stripe_ref'


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        # Fresh migration-only DB: build the table from the model (it already
        # carries stripe_ref + its index), and we're done.
        SQLModel.metadata.tables[_TABLE].create(bind)
        return

    cols = {c['name'] for c in sa.inspect(bind).get_columns(_TABLE)}
    if 'stripe_ref' not in cols:
        op.add_column(_TABLE, sa.Column(
            'stripe_ref', sqlmodel.sql.sqltypes.AutoString(),
            nullable=False, server_default=''))
        op.create_index(_INDEX, _TABLE, ['stripe_ref'])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, 'stripe_ref')
