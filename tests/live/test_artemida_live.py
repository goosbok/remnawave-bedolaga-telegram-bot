"""Живой смоук Artemida /v1: подтверждает контракт клиента против настоящего API.

Запуск: ``ARTEMIDA_LIVE=1 ARTEMIDA_API_KEY=artm_live_… uv run pytest -m artemida_live tests/live -q``.
Без ``ARTEMIDA_LIVE=1`` — пропускается целиком, боевой прод-бот не участвует.

ВНИМАНИЕ: тест тратит 2 ₽ с API-баланса вендора (создаёт один trial-ключ) и
затем его отзывает. Это единственное место в наборе, которое реально ходит к
вендору и списывает деньги — потому оно за флагом и вне обычного ``make test``.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.external.artemida_api import ArtemidaClient


ENV_FLAG = 'ARTEMIDA_LIVE'
ENV_KEY = 'ARTEMIDA_API_KEY'

pytestmark = [
    pytest.mark.artemida_live,
    pytest.mark.skipif(
        os.environ.get(ENV_FLAG) != '1' or not os.environ.get(ENV_KEY),
        reason=f'{ENV_FLAG}=1 и {ENV_KEY} не заданы — живой API и деньги не трогаем',
    ),
]


@pytest.fixture
def api_key() -> str:
    return os.environ[ENV_KEY]


async def test_trial_lifecycle_against_real_api(api_key: str) -> None:
    """balance → создать trial (2 ₽) → subscription-links → отозвать ключ."""
    idem = f'live-trial-{uuid.uuid4().hex}'
    async with ArtemidaClient(api_key=api_key) as client:
        balance = await client.get_balance()
        assert 'balance' in balance

        key = await client.create_trial(idempotency_key=idem)
        assert key.id

        links = await client.get_subscription_links(key.id)
        assert isinstance(links.get('links'), list)

        revoke = await client.revoke_key(key.id, idempotency_key=f'{idem}-revoke')
        assert revoke is not None
