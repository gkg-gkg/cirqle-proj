"""add receipt.image_sha256 for duplicate-image detection

Revision ID: a1c7f3e9b204
Revises: 484de2eac020
Create Date: 2026-08-27

Existing rows get '' (we never stored the bytes' hash before), so duplicate
detection only covers receipts uploaded after this migration runs.
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'a1c7f3e9b204'
down_revision: Union[str, Sequence[str], None] = '484de2eac020'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('receipt', sa.Column(
        'image_sha256', sqlmodel.sql.sqltypes.AutoString(),
        nullable=False, server_default=''))


def downgrade() -> None:
    op.drop_column('receipt', 'image_sha256')
