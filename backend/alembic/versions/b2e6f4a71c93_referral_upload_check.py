"""record the referral as typed, and whether the referrer's own claim is approved

Revision ID: b2e6f4a71c93
Revises: f9a3c07e42b1
Create Date: 2026-09-03

A referral is only credible if the referrer claimed the same deal themselves, so
the upload now checks for that claim and stores what it found. Two columns:

  referred_by_handle — the handle as the member typed it. referred_by_user_id is
    the truth, but a member can change their handle later, and a dispute has to
    be judged on what was actually entered at the time.
  referral_status — '' no referral | 'pending' the referrer has claimed this deal
    but the admin hasn't approved it | 'verified' approved.

Existing rows get '' for both. That is honest rather than lossy: claims uploaded
before this check ran were never tested against it, and back-filling 'verified'
would invent a check that never happened.
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'b2e6f4a71c93'
down_revision: Union[str, Sequence[str], None] = 'f9a3c07e42b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('receipt', sa.Column(
        'referred_by_handle', sqlmodel.sql.sqltypes.AutoString(),
        nullable=False, server_default=''))
    op.add_column('receipt', sa.Column(
        'referral_status', sqlmodel.sql.sqltypes.AutoString(),
        nullable=False, server_default=''))


def downgrade() -> None:
    op.drop_column('receipt', 'referral_status')
    op.drop_column('receipt', 'referred_by_handle')
