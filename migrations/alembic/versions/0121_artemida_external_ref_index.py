"""partial unique index on subscriptions.external_ref (Artemida)

The public rebrand-subscription route (``GET /a/{token}``) resolves a
subscription by ``external_ref`` on every request. Without an index that is
a seq-scan over the whole ``subscriptions`` table, and without a uniqueness
guarantee two rows could end up sharing the same vendor key id, which would
make ``scalar_one_or_none()`` in the route raise ``MultipleResultsFound``
instead of behaving.

The index is partial (``external_provider = 'artemida'``) the same way
``uq_subscriptions_remnawave_id`` (see 0104) is partial on
``remnawave_id IS NOT NULL`` — rows from other providers, and rows that never
had an external ref, don't participate in the uniqueness check.

Guarded per the repo convention (0104/0117/0118/0120): revision ``0001``
builds fresh schemas via ``Base.metadata.create_all`` from the CURRENT
``models.py``, so on a freshly created database this index already exists by
the time this revision runs.

Revision ID: 0121
Revises: 0120
Create Date: 2026-09-19

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0121'
down_revision: Union[str, None] = '0120'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = 'uq_subscriptions_artemida_external_ref'
_PARTIAL_WHERE = "external_provider = 'artemida'"


def _index_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {str(item['name']) for item in inspector.get_indexes(table) if item.get('name')}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'subscriptions' not in inspector.get_table_names():
        return

    if _INDEX_NAME not in _index_names(inspector, 'subscriptions'):
        op.create_index(
            _INDEX_NAME,
            'subscriptions',
            ['external_ref'],
            unique=True,
            postgresql_where=sa.text(_PARTIAL_WHERE),
            sqlite_where=sa.text(_PARTIAL_WHERE),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'subscriptions' not in inspector.get_table_names():
        return

    if _INDEX_NAME in _index_names(inspector, 'subscriptions'):
        op.drop_index(_INDEX_NAME, table_name='subscriptions')
