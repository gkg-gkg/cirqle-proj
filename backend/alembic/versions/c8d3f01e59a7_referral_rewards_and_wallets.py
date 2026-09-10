"""referral rewards: the £1 ledger, two merchant wallets, identity fingerprints

Revision ID: c8d3f01e59a7
Revises: b2e6f4a71c93
Create Date: 2026-09-03

Three things, all needed before a referral can pay anything:

  merchanttransaction.wallet — merchants now fund two separate pots, one for
    cashback and one for referral bonuses. Existing rows are 'cashback', which
    is what they were: every top-up so far was to pay cashback.

  referralreward — the bonuses themselves. A genuine referral pays twice: £1 to
    the referrer and 50p to the person they referred, both hanging off the
    REFERRED claim. Rows of their own rather than money added onto a receipt, so
    cashback and referral money stay tellable apart on the merchant's statement
    and a cancelled bonus never disturbs cashback. UNIQUE on (receipt_id, kind):
    one claim carries at most one reward per side, which is what stops a retry or
    a double-settle paying twice.

  campaign.referrals_enabled — a merchant opts each deal in deliberately. Off for
    every existing deal, so funding a wallet never silently enrols anything.

  user.payout_fingerprint / identity_fingerprint — hashes of what Stripe already
    verified about a member (their bank account, and their name + date of birth),
    so two accounts belonging to one person can't refer each other. Hashes only:
    enough to compare two members, useless to anyone who reads the database.

Drift-aware, like e2b9d47c1a05 before it. `merchanttransaction` predates the
migration chain, and on a database built by migrations alone that earlier
revision creates the table FROM THE MODEL — which now already has `wallet`. So
every addition here checks first rather than assuming production's shape.
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'c8d3f01e59a7'
down_revision: Union[str, Sequence[str], None] = 'b2e6f4a71c93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STR = sqlmodel.sql.sqltypes.AutoString


def _columns(bind, table: str) -> set:
    return {c['name'] for c in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set:
    return {i['name'] for i in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()

    if 'wallet' not in _columns(bind, 'merchanttransaction'):
        op.add_column('merchanttransaction', sa.Column(
            'wallet', _STR(), nullable=False, server_default='cashback'))

    user_cols = _columns(bind, 'user')
    if 'payout_fingerprint' not in user_cols:
        op.add_column('user', sa.Column(
            'payout_fingerprint', _STR(), nullable=False, server_default=''))
    if 'identity_fingerprint' not in user_cols:
        op.add_column('user', sa.Column(
            'identity_fingerprint', _STR(), nullable=False, server_default=''))

    user_idx = _indexes(bind, 'user')
    if 'ix_user_payout_fingerprint' not in user_idx:
        op.create_index('ix_user_payout_fingerprint', 'user', ['payout_fingerprint'])
    if 'ix_user_identity_fingerprint' not in user_idx:
        op.create_index('ix_user_identity_fingerprint', 'user', ['identity_fingerprint'])

    if 'referrals_enabled' not in _columns(bind, 'campaign'):
        op.add_column('campaign', sa.Column(
            'referrals_enabled', sa.Boolean(), nullable=False,
            server_default=sa.false()))

    if sa.inspect(bind).has_table('referralreward'):
        return

    op.create_table(
        'referralreward',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', _STR(), nullable=False),
        sa.Column('receipt_id', sa.Integer(), nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=True),
        sa.Column('merchant_id', sa.Integer(), nullable=True),
        sa.Column('amount', sa.Float(), nullable=False, server_default='0'),
        sa.Column('status', _STR(), nullable=False, server_default='available'),
        sa.Column('cancel_reason', _STR(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.ForeignKeyConstraint(['receipt_id'], ['receipt.id']),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaign.id']),
        sa.ForeignKeyConstraint(['merchant_id'], ['merchant.id']),
        sa.PrimaryKeyConstraint('id'),
        # One reward per side per referred claim. This is the guard that makes
        # settling safe to re-run: a second attempt hits the constraint, not a
        # payment.
        sa.UniqueConstraint('receipt_id', 'kind',
                            name='uq_referralreward_receipt_kind'),
    )
    op.create_index('ix_referralreward_user_id', 'referralreward', ['user_id'])
    op.create_index('ix_referralreward_kind', 'referralreward', ['kind'])
    op.create_index('ix_referralreward_receipt_id', 'referralreward', ['receipt_id'])
    op.create_index('ix_referralreward_merchant_id', 'referralreward', ['merchant_id'])
    op.create_index('ix_referralreward_status', 'referralreward', ['status'])


def downgrade() -> None:
    op.drop_index('ix_referralreward_status', table_name='referralreward')
    op.drop_index('ix_referralreward_merchant_id', table_name='referralreward')
    op.drop_index('ix_referralreward_receipt_id', table_name='referralreward')
    op.drop_index('ix_referralreward_kind', table_name='referralreward')
    op.drop_index('ix_referralreward_user_id', table_name='referralreward')
    op.drop_table('referralreward')
    op.drop_column('campaign', 'referrals_enabled')

    op.drop_index('ix_user_identity_fingerprint', table_name='user')
    op.drop_index('ix_user_payout_fingerprint', table_name='user')
    op.drop_column('user', 'identity_fingerprint')
    op.drop_column('user', 'payout_fingerprint')

    op.drop_column('merchanttransaction', 'wallet')
