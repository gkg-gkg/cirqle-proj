"""referral competition: opt-in, display name, and frozen season standings

Revision ID: a7e4c2b8d016
Revises: e83c26d1f409
Create Date: 2026-09-10

Adds what the national referral leaderboard needs and nothing else.

`leaderboard_opt_in` defaults to FALSE for everyone, existing members included.
The Instagram handle was collected so referrals could be credited and posts
matched — publishing it in a ranked table is a different bargain, so nobody is
entered into the competition by a migration.

`display_name` is blank by default and falls back to the handle, so opting in
never forces a member to invent a name first.

`seasonstanding` stores a season's table ONCE, after the season has closed and
its last claims have cleared. A running season has no rows here — it is counted
fresh on every read. The table exists because a £10 prize hangs off the final
order, and scores keep moving for days after a season ends: cashback clears
three days after the post date, and the admin queue drains after that. Freezing
is what stops a rank changing after the member has been told they won.

The UNIQUE on (season, user_id) is what makes freezing safe to reach twice. Two
requests can arrive at the freeze date together; the second one fails its insert
rather than writing a second, differently-ordered copy of the table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'a7e4c2b8d016'
down_revision: Union[str, Sequence[str], None] = 'e83c26d1f409'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user', sa.Column(
        'leaderboard_opt_in', sa.Boolean(), nullable=False,
        server_default=sa.false()))
    op.add_column('user', sa.Column(
        'display_name', sqlmodel.sql.sqltypes.AutoString(),
        nullable=False, server_default=''))

    op.create_table(
        'seasonstanding',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('season', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.Column('score', sa.Integer(), nullable=False),
        sa.Column('frozen_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('season', 'user_id',
                            name='uq_seasonstanding_season_user'),
    )
    op.create_index('ix_seasonstanding_season', 'seasonstanding', ['season'])
    op.create_index('ix_seasonstanding_user_id', 'seasonstanding', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_seasonstanding_user_id', table_name='seasonstanding')
    op.drop_index('ix_seasonstanding_season', table_name='seasonstanding')
    op.drop_table('seasonstanding')

    # Batched rather than two plain drops: SQLite below 3.35 cannot DROP COLUMN
    # and the local runtime is 3.34 (see d41f7b0c8e35).
    with op.batch_alter_table('user') as batch:
        batch.drop_column('display_name')
        batch.drop_column('leaderboard_opt_in')
