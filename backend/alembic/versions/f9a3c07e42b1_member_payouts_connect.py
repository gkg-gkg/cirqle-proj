"""add member payouts: Stripe Connect account fields + payout ledger

Revision ID: f9a3c07e42b1
Revises: e7c25b91af38
Create Date: 2026-09-01

Paying a member real money requires Stripe to verify them first, so each member
gets a Connect account. We store only the account id and whether Stripe has
cleared them — never bank details, which stay with Stripe.

`payout` records each withdrawal so the money and the receipt ledger can always
be reconciled against each other.
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'f9a3c07e42b1'
down_revision: Union[str, Sequence[str], None] = 'e7c25b91af38'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('user')}
    if 'stripe_account_id' not in existing:
        op.add_column('user', sa.Column('stripe_account_id',
                                        sqlmodel.sql.sqltypes.AutoString(),
                                        nullable=False, server_default=''))
        op.create_index('ix_user_stripe_account_id', 'user', ['stripe_account_id'])
    for col in ('payouts_enabled', 'payout_details_submitted'):
        if col not in existing:
            op.add_column('user', sa.Column(col, sa.Boolean(), nullable=False,
                                            server_default=sa.false()))

    if not sa.inspect(op.get_bind()).has_table('payout'):
        op.create_table(
            'payout',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('amount', sa.Float(), nullable=False),
            sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column('stripe_transfer_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column('failure_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['user_id'], ['user.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_payout_user_id', 'payout', ['user_id'])
        op.create_index('ix_payout_stripe_transfer_id', 'payout', ['stripe_transfer_id'])


def downgrade() -> None:
    op.drop_table('payout')
    op.drop_index('ix_user_stripe_account_id', table_name='user')
    for col in ('payout_details_submitted', 'payouts_enabled', 'stripe_account_id'):
        op.drop_column('user', col)
