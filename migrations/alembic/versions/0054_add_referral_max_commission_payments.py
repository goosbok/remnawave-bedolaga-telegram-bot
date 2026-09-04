"""add per-user referral commission payment limit

Revision ID: 0054
Revises: 0053
Create Date: 2026-09-04

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0054'
down_revision: Union[str, None] = '0053'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NULL = решает статус: партнёр получает комиссию со всех оплат,
    # обычный пользователь — по глобальному REFERRAL_MAX_COMMISSION_PAYMENTS.
    # 0 = без ограничения, N > 0 = только первые N оплат каждого реферала.
    op.add_column('users', sa.Column('referral_max_commission_payments', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'referral_max_commission_payments')
