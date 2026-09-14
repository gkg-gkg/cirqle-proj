"""brand tag attribution: which brand a post actually tagged

Revision ID: a4f2c81d7b60
Revises: d2f7a4c8e1b6
Create Date: 2026-09-14

A post is scraped because it tags @cirqle.co.uk, which makes it a Cirqle post
but says nothing about which BRAND it is about. The member said that separately,
by picking a campaign at receipt upload, and nothing checked the two agreed —
so tagging one brand while claiming another brand's deal billed the wrong
merchant for a post that never named them.

`mention.tagged_handles` stores the other accounts a post tags, as a JSON list
of normalised handles in TEXT (the same trick as campaign.tags, so the shape is
identical on SQLite and Postgres). It is NULLABLE on purpose: NULL means we
never captured this post's tags, which is not the same as '[]', meaning we read
the post and it tagged nobody. Every existing row is NULL — those posts were
scraped before this column existed — and app/brandtags.py treats NULL as no
evidence, so no member's existing claim is blocked by it. Rows fill in as
members refresh their feed.

`receipt.tag_match` records how the claim compared, for the admin reviewer.
Existing rows get '', the same value a visit claim carries, since a claim
uploaded before the check ran was never evaluated.
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'a4f2c81d7b60'
down_revision: Union[str, Sequence[str], None] = 'd2f7a4c8e1b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('mention', sa.Column(
        'tagged_handles', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('receipt', sa.Column(
        'tag_match', sqlmodel.sql.sqltypes.AutoString(),
        nullable=False, server_default=''))


def downgrade() -> None:
    # Batched rather than plain drops: SQLite below 3.35 cannot DROP COLUMN,
    # and the local runtime is 3.34 (see d41f7b0c8e35).
    with op.batch_alter_table('receipt') as batch:
        batch.drop_column('tag_match')
    with op.batch_alter_table('mention') as batch:
        batch.drop_column('tagged_handles')
