"""provider fields for external-vendor tariffs (Artemida)

Revision ID: 0120
Revises: 0119
Create Date: 2026-09-18

Ключевая колонка новой фичи: ``tariffs.provider`` — 'remnawave' (свои ноды,
дефолт) | 'artemida' (внешний вендор ARTΞMIDA). ``tariffs.provider_opts``
несёт провайдер-специфичные настройки (напр. пиннинг локаций Artemida).
На ``subscriptions`` — ``external_provider``/``external_ref``: ключ подписки
у внешнего вендора, заполняется только когда ``provider='artemida'``.

Колонки-гварды по образцу 0117/0118: ревизия 0001 сама вызывает
``Base.metadata.create_all`` из ТЕКУЩЕГО models.py, поэтому на свежесобранной
базе эти колонки уже есть до этой миграции — без гварда ``add_column`` упал
бы с DuplicateColumn.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0120'
down_revision: Union[str, None] = '0119'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return True  # таблицы нет — создастся уже с колонкой
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_column('tariffs', 'provider'):
        with op.batch_alter_table('tariffs') as batch:
            batch.add_column(sa.Column('provider', sa.String(20), nullable=False, server_default='remnawave'))
    if not _has_column('tariffs', 'provider_opts'):
        with op.batch_alter_table('tariffs') as batch:
            batch.add_column(sa.Column('provider_opts', sa.JSON(), nullable=False, server_default='{}'))

    if not _has_column('subscriptions', 'external_provider'):
        with op.batch_alter_table('subscriptions') as batch:
            batch.add_column(sa.Column('external_provider', sa.String(20), nullable=True))
    if not _has_column('subscriptions', 'external_ref'):
        with op.batch_alter_table('subscriptions') as batch:
            batch.add_column(sa.Column('external_ref', sa.String(255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('subscriptions') as batch:
        batch.drop_column('external_ref')
        batch.drop_column('external_provider')
    with op.batch_alter_table('tariffs') as batch:
        batch.drop_column('provider_opts')
        batch.drop_column('provider')
