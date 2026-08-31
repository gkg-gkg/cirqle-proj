"""referral attribution: receipt.referred_by_user_id + case-insensitive
unique instagram handles

Revision ID: f3b8a1d9e2c5
Revises: a1c7f3e9b204
Create Date: 2026-08-31

Referral attribution resolves a handle typed at claim time back to one
`User`, so `instagram_handle` needs to reliably identify a single account.
This backfills/re-normalizes any handle that predates the app-level
normalization, then adds a case-insensitive unique index. The index is
partial — blank ("") handles, the default for users who never set one, are
excluded rather than treated as a collision. If normalization surfaces two
users who now share a handle, the migration aborts instead of silently
picking a winner; that has to be resolved by hand first.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f3b8a1d9e2c5'
down_revision: Union[str, Sequence[str], None] = 'a1c7f3e9b204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


user_table = sa.table(
    'user',
    sa.column('id', sa.Integer),
    sa.column('instagram_handle', sa.String),
)


def _normalize(raw) -> str:
    return (raw or "").strip().lstrip("@").lower()


def upgrade() -> None:
    # Batch mode: SQLite can't ALTER a table to add a foreign-key constraint
    # in place (only Postgres can), so this uses the copy-and-move strategy
    # there; on Postgres it just runs the equivalent ALTER statements.
    with op.batch_alter_table('receipt', schema=None) as batch_op:
        batch_op.add_column(sa.Column('referred_by_user_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_receipt_referred_by_user_id', 'user', ['referred_by_user_id'], ['id'])
        batch_op.create_index(
            op.f('ix_receipt_referred_by_user_id'), ['referred_by_user_id'], unique=False)

    bind = op.get_bind()
    rows = bind.execute(
        sa.select(user_table.c.id, user_table.c.instagram_handle)
    ).fetchall()

    by_normalized = {}
    for user_id, handle in rows:
        normalized = _normalize(handle)
        if normalized != (handle or ""):
            bind.execute(
                user_table.update()
                .where(user_table.c.id == user_id)
                .values(instagram_handle=normalized)
            )
        if normalized:
            by_normalized.setdefault(normalized, []).append(user_id)

    collisions = {h: ids for h, ids in by_normalized.items() if len(ids) > 1}
    if collisions:
        detail = "; ".join(f"'{h}' -> user ids {ids}" for h, ids in collisions.items())
        raise RuntimeError(
            "Cannot add the case-insensitive unique index on user.instagram_handle: "
            f"duplicate handles must be resolved manually first: {detail}"
        )

    op.execute(
        'CREATE UNIQUE INDEX ix_user_instagram_handle_ci '
        'ON "user" (lower(instagram_handle)) '
        "WHERE instagram_handle <> ''"
    )


def downgrade() -> None:
    op.execute('DROP INDEX ix_user_instagram_handle_ci')
    with op.batch_alter_table('receipt', schema=None) as batch_op:
        batch_op.drop_index(op.f('ix_receipt_referred_by_user_id'))
        batch_op.drop_constraint('fk_receipt_referred_by_user_id', type_='foreignkey')
        batch_op.drop_column('referred_by_user_id')
