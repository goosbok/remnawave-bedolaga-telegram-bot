"""add per-user referral commission payment limit

Revision ID: 0119
Revises: 0118
Create Date: 2026-09-04

Renumbered from 0054 to 0119 when merging upstream (2026-09-10): upstream
independently used revision 0054 for 0054_add_broadcast_category.py, and by
the time of this merge upstream's chain had advanced to 0118. Content
unchanged — only revision/down_revision/filename moved to graft onto the
merged head instead of branching at 0053.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0119'
down_revision: Union[str, None] = '0118'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NULL = решает статус: партнёр получает комиссию со всех оплат,
    # обычный пользователь — по глобальному REFERRAL_MAX_COMMISSION_PAYMENTS.
    # 0 = без ограничения, N > 0 = только первые N оплат каждого реферала.
    op.add_column('users', sa.Column('referral_max_commission_payments', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'referral_max_commission_payments')
