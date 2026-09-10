"""email verification + password reset: authtoken table, verification and
password_changed_at columns on user and merchant

Revision ID: d5e1a8c30b47
Revises: b7d4c9e15f80
Create Date: 2026-09-01

Adds the machinery for emailed link tokens. Three things to know:

1. `authtoken` is shared by members and merchants (`subject_type` picks the
   table), and stores only the SHA-256 hash of each token.
2. `password_changed_at` is what makes a password reset log out other devices:
   login tokens carry this value and stop being accepted when it moves. Existing
   rows are backfilled to their `created_at`, so tokens issued before this
   deploy keep working until they expire naturally rather than logging everyone
   out on deploy day.
3. Grandfathering: every account that can sign in TODAY must still sign in
   tomorrow. So existing users are marked email-verified, and any user still at
   the old default 'pending' keeps that status — the new 'unverified' state
   applies only to sign-ups from here on. Merchants are all marked verified with
   must_set_password false, since they already have working passwords.
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'd5e1a8c30b47'
down_revision: Union[str, Sequence[str], None] = 'b7d4c9e15f80'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'authtoken',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('subject_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('subject_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('new_email', sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False, server_default=''),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_authtoken_subject_id', 'authtoken', ['subject_id'])
    # Unique: a token hash identifies exactly one row, so redeeming is a single
    # indexed lookup and a duplicate can never be issued.
    op.create_index('ix_authtoken_token_hash', 'authtoken', ['token_hash'], unique=True)

    for table in ('user', 'merchant'):
        op.add_column(table, sa.Column('email_verified_at', sa.DateTime(), nullable=True))
        op.add_column(table, sa.Column(
            'pending_email', sqlmodel.sql.sqltypes.AutoString(),
            nullable=False, server_default=''))
        op.add_column(table, sa.Column('password_changed_at', sa.DateTime(), nullable=True))
    op.add_column('merchant', sa.Column(
        'must_set_password', sa.Boolean(), nullable=False, server_default=sa.false()))

    # ── Grandfather every existing account ──
    # Quoted "user" because it's a reserved word in Postgres.
    op.execute('UPDATE "user" SET email_verified_at = created_at, '
               'password_changed_at = created_at')
    op.execute('UPDATE merchant SET email_verified_at = created_at, '
               'password_changed_at = created_at')

    # password_changed_at is deliberately left NULLABLE at the database level.
    # Tightening it to NOT NULL would need batch_alter_table, and on SQLite that
    # rebuilds the whole table by reflecting it — which silently DROPS the
    # expression-based partial index ix_user_instagram_handle_ci (SQLAlchemy
    # cannot reflect expression indexes). Losing that index would break the
    # one-handle-one-account guarantee referral attribution relies on.
    # Instead: the backfill above gives every existing row a value, the model's
    # default_factory gives every new row one, and security.py treats a NULL as
    # "never changed" via `password_changed_at or created_at`.


def downgrade() -> None:
    op.drop_column('merchant', 'must_set_password')
    for table in ('user', 'merchant'):
        op.drop_column(table, 'password_changed_at')
        op.drop_column(table, 'pending_email')
        op.drop_column(table, 'email_verified_at')
    op.drop_index('ix_authtoken_token_hash', table_name='authtoken')
    op.drop_index('ix_authtoken_subject_id', table_name='authtoken')
    op.drop_table('authtoken')
