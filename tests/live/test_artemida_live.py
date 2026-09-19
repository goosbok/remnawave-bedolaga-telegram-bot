"""Живой смоук Artemida /v1: подтверждает контракт клиента против настоящего API.

Запуск: ``ARTEMIDA_LIVE=1 ARTEMIDA_API_KEY=artm_live_… uv run pytest -m artemida_live tests/live -q``.
Без ``ARTEMIDA_LIVE=1`` — пропускается целиком, боевой прод-бот не участвует.

ВНИМАНИЕ: тест тратит 2 ₽ с API-баланса вендора (создаёт один trial-ключ) и
затем его ОТЗЫВАЕТ в ``finally`` — даже если середина упадёт. Это единственное
место в наборе, которое реально ходит к вендору и списывает деньги, потому оно
за флагом и вне обычного ``make test``.

Ключ читается внутри теста из окружения (НЕ через фикстуру-параметр), чтобы
pytest не печатал его в трейсбэке при падении.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from app.external.artemida_api import ArtemidaAPIError, ArtemidaClient


ENV_FLAG = 'ARTEMIDA_LIVE'
ENV_KEY = 'ARTEMIDA_API_KEY'

pytestmark = [
    pytest.mark.artemida_live,
    pytest.mark.skipif(
        os.environ.get(ENV_FLAG) != '1' or not os.environ.get(ENV_KEY),
        reason=f'{ENV_FLAG}=1 и {ENV_KEY} не заданы — живой API и деньги не трогаем',
    ),
]


async def _links_with_retry(client: ArtemidaClient, key_id: str, *, attempts: int = 4, delay: float = 3.0) -> dict:
    """subscription-сервер вендора у свежего ключа бывает недоступен пару секунд — ретраим транзиент."""
    last_error: ArtemidaAPIError | None = None
    for attempt in range(attempts):
        try:
            return await client.get_subscription_links(key_id)
        except ArtemidaAPIError as error:
            last_error = error
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
    raise last_error  # type: ignore[misc]


async def test_trial_lifecycle_against_real_api() -> None:
    """balance → создать trial (2 ₽) → subscription-links (с ретраем) → всегда отозвать ключ."""
    api_key = os.environ[ENV_KEY]
    idem = f'live-trial-{uuid.uuid4().hex}'
    async with ArtemidaClient(api_key=api_key) as client:
        balance = await client.get_balance()
        assert 'balance' in balance

        key = await client.create_trial(idempotency_key=idem)
        assert key.id

        try:
            links = await _links_with_retry(client, key.id)
            assert isinstance(links.get('links'), list)
        finally:
            # Всегда убираем оплаченный trial, даже если проверка ссылок упала.
            await client.revoke_key(key.id, idempotency_key=f'{idem}-revoke')
