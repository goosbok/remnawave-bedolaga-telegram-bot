"""stable public_token on subscriptions (vendor-agnostic link id)

A later task builds the client subscription link on ``subscriptions.public_token``
and resolves the public rebrand route by it, so swapping the vendor
(remnawave/artemida) never changes the client's URL. This revision only adds
the column and its unique lookup index — nothing reads or writes it yet.

Guarded per the repo convention (0104/0117/0118/0120/0121): revision ``0001``
builds fresh schemas via ``Base.metadata.create_all`` from the CURRENT
``models.py``, so on a freshly created database this column and index already
exist by the time this revision runs.

Revision ID: 0122
Revises: 0121
Create Date: 2026-09-19

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0122'
down_revision: Union[str, None] = '0121'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = 'ix_subscriptions_public_token'


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return True  # таблицы нет — создастся уже с колонкой
    return column in [c['name'] for c in inspector.get_columns(table)]


def _index_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {str(item['name']) for item in inspector.get_indexes(table) if item.get('name')}


def upgrade() -> None:
    if not _has_column('subscriptions', 'public_token'):
        with op.batch_alter_table('subscriptions') as batch:
            batch.add_column(sa.Column('public_token', sa.String(64), nullable=True))

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'subscriptions' in inspector.get_table_names() and _INDEX_NAME not in _index_names(inspector, 'subscriptions'):
        op.create_index(_INDEX_NAME, 'subscriptions', ['public_token'], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'subscriptions' in inspector.get_table_names() and _INDEX_NAME in _index_names(inspector, 'subscriptions'):
        op.drop_index(_INDEX_NAME, table_name='subscriptions')

    if _has_column('subscriptions', 'public_token'):
        with op.batch_alter_table('subscriptions') as batch:
            batch.drop_column('public_token')
