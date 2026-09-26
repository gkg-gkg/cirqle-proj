"""campaign stores: physical store locations for "deals near me"

Revision ID: b7e3d9f1a2c4
Revises: a4f2c81d7b60
Create Date: 2026-09-26

`campaign.stores` is a JSON list of {"name", "postcode", "lat", "lng"} in TEXT
(the same trick as campaign.tags). Every existing deal gets '[]': none has
store data yet, and "no stores" is exactly what the browse page must say.
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'b7e3d9f1a2c4'
down_revision: Union[str, Sequence[str], None] = 'a4f2c81d7b60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('campaign', sa.Column(
        'stores', sqlmodel.sql.sqltypes.AutoString(),
        nullable=False, server_default='[]'))


def downgrade() -> None:
    # Batched: SQLite below 3.35 cannot DROP COLUMN (see a4f2c81d7b60).
    with op.batch_alter_table('campaign') as batch:
        batch.drop_column('stores')
