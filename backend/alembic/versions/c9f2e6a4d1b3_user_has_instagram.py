"""user.has_instagram: members without Instagram pick a Cirqle username instead

Revision ID: c9f2e6a4d1b3
Revises: b7e3d9f1a2c4
Create Date: 2026-09-28

A member who doesn't use Instagram still needs a unique handle, because a friend
types it into their claim to credit a referral. It lives in the same
instagram_handle column (so the existing case-insensitive uniqueness index
covers both), and this flag says it is NOT an Instagram account — so the feed
never matches real Instagram posts to someone who only picked that name.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c9f2e6a4d1b3'
down_revision: Union[str, Sequence[str], None] = 'b7e3d9f1a2c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('user')}
    if 'has_instagram' not in existing:
        op.add_column('user', sa.Column('has_instagram', sa.Boolean(), nullable=False,
                                        server_default=sa.true()))


def downgrade() -> None:
    op.drop_column('user', 'has_instagram')
