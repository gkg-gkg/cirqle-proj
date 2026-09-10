"""receipt vision verification: merchant reference receipt + HITL scoring

Revision ID: c7e9a2f5b1d4
Revises: b3d7f1a4c8e9
Create Date: 2026-09-10

Adds the columns behind Claude-vision receipt verification (see
app/receipt_verification.py): a merchant-level reference receipt (compared
against every claim on their campaigns), and per-receipt authenticity /
purchase-match scores plus the human-in-the-loop decision trail. This
replaces app/verify.py's Textract check as the automatic background pipeline
— verify.py itself is untouched and still reachable manually.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c7e9a2f5b1d4'
down_revision: Union[str, Sequence[str], None] = 'b3d7f1a4c8e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('merchant', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reference_receipt_s3_key', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('reference_fields', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('reference_status', sa.String(), nullable=False,
                                      server_default='pending_extraction'))

    with op.batch_alter_table('receipt', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ocr_fields', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('image_hash', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('authenticity_score', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('authenticity_detail', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('purchase_match_score', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('purchase_match_detail', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('overall_score', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('verification_status', sa.String(), nullable=False,
                                      server_default='pending'))
        batch_op.add_column(sa.Column('verification_error', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('decision_source', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('decision_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('cashback_triggered_at', sa.DateTime(), nullable=True))

    op.create_index(op.f('ix_receipt_verification_status'), 'receipt', ['verification_status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_receipt_verification_status'), table_name='receipt')

    with op.batch_alter_table('receipt', schema=None) as batch_op:
        batch_op.drop_column('cashback_triggered_at')
        batch_op.drop_column('decision_at')
        batch_op.drop_column('decision_source')
        batch_op.drop_column('verification_error')
        batch_op.drop_column('verification_status')
        batch_op.drop_column('overall_score')
        batch_op.drop_column('purchase_match_detail')
        batch_op.drop_column('purchase_match_score')
        batch_op.drop_column('authenticity_detail')
        batch_op.drop_column('authenticity_score')
        batch_op.drop_column('image_hash')
        batch_op.drop_column('ocr_fields')

    with op.batch_alter_table('merchant', schema=None) as batch_op:
        batch_op.drop_column('reference_status')
        batch_op.drop_column('reference_fields')
        batch_op.drop_column('reference_receipt_s3_key')
