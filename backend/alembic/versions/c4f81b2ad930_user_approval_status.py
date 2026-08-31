"""add user.status — new sign-ups need admin approval

Revision ID: c4f81b2ad930
Revises: a1c7f3e9b204
Create Date: 2026-08-31

The column defaults to 'pending' (so an account is never usable until someone
approves it), and accounts that already existed are then grandfathered to
'approved' — the gate only applies "from now on".
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'c4f81b2ad930'
down_revision: Union[str, Sequence[str], None] = 'a1c7f3e9b204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user', sa.Column(
        'status', sqlmodel.sql.sqltypes.AutoString(),
        nullable=False, server_default='pending'))
    # Everyone who signed up before the gate existed keeps their access.
    op.execute("UPDATE \"user\" SET status = 'approved'")


def downgrade() -> None:
    op.drop_column('user', 'status')
