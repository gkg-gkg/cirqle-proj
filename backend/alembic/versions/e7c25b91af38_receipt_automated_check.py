"""add receipt automated-check columns

Revision ID: e7c25b91af38
Revises: d5e1a8c30b47
Create Date: 2026-09-01

Filled in the background by app/verify.py shortly after upload. Advisory only —
nothing reads these to decide anything yet, they are shown to the admin next to
the receipt image. Existing rows get check_status '' meaning "never checked".

receipt_number is indexed but NOT unique: extraction can misread a number, and
a unique constraint would reject honest uploads while this is still advisory.
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'e7c25b91af38'
down_revision: Union[str, Sequence[str], None] = 'd5e1a8c30b47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('receipt', sa.Column(
        'check_status', sqlmodel.sql.sqltypes.AutoString(),
        nullable=False, server_default=''))
    op.add_column('receipt', sa.Column(
        'check_score', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('receipt', sa.Column(
        'check_data', sqlmodel.sql.sqltypes.AutoString(),
        nullable=False, server_default='{}'))
    op.add_column('receipt', sa.Column(
        'checked_at', sa.DateTime(), nullable=True))
    op.add_column('receipt', sa.Column(
        'receipt_number', sqlmodel.sql.sqltypes.AutoString(),
        nullable=False, server_default=''))
    op.create_index('ix_receipt_receipt_number', 'receipt', ['receipt_number'])


def downgrade() -> None:
    op.drop_index('ix_receipt_receipt_number', table_name='receipt')
    for column in ('receipt_number', 'checked_at', 'check_data',
                   'check_score', 'check_status'):
        op.drop_column('receipt', column)
