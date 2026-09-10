"""add per-user referral commission payment limit

Revision ID: 0119
Revises: 0118
Create Date: 2026-09-04

Renumbered from 0054 to 0119 when merging upstream (2026-09-10): upstream
independently used revision 0054 for 0054_add_broadcast_category.py, and by
the time of this merge upstream's chain had advanced to 0118.

This is not just a filename/id move. Confirmed by dry-running this chain
against a copy of the real production DB: that database's `alembic_version`
already says '0054', stamped there by OUR OLD migration (this one) under the
original numbering. Because upstream's 0054_add_broadcast_category.py now
owns that revision id, alembic treats it as already applied on our
production DB and skips it — `broadcast_history.category` never gets
created there, and our own column add below would fail with
DuplicateColumnError on a genuinely-already-migrated install (revision id
reused, but each side's DB took a different fork of history at 0053).

Both column adds are therefore inspector-guarded so this migration
reconciles either fork:
- our production DB (has the column, needs the reconciliation): backfills
  the missing broadcast_history.category, skips the already-present column.
- a fresh install / any environment that took upstream's 0054 in sequence
  (has broadcast_history.category, needs the reconciliation the other way):
  skips the already-present column, adds our referral column for the first
  time.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0119'
down_revision: Union[str, None] = '0118'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    users_columns = {c['name'] for c in inspector.get_columns('users')}
    if 'referral_max_commission_payments' not in users_columns:
        # NULL = решает статус: партнёр получает комиссию со всех оплат,
        # обычный пользователь — по глобальному REFERRAL_MAX_COMMISSION_PAYMENTS.
        # 0 = без ограничения, N > 0 = только первые N оплат каждого реферала.
        op.add_column('users', sa.Column('referral_max_commission_payments', sa.Integer(), nullable=True))

    broadcast_columns = {c['name'] for c in inspector.get_columns('broadcast_history')}
    if 'category' not in broadcast_columns:
        # Upstream's 0054_add_broadcast_category, replayed here for installs
        # that took our fork of the collided revision id instead (see module
        # docstring) and therefore never actually ran it.
        op.add_column(
            'broadcast_history',
            sa.Column('category', sa.String(20), nullable=False, server_default='system'),
        )


def downgrade() -> None:
    # Only reverts our own column. broadcast_history.category is left alone:
    # on a fork that took upstream's real 0054 in sequence, that column
    # belongs to THAT migration's downgrade(), not this reconciliation one.
    op.drop_column('users', 'referral_max_commission_payments')
