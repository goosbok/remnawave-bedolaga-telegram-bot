"""Живой e2e серверной цепочки Artemida: реальный ключ → реальные ссылки → наш ребренд.

Запуск: ``ARTEMIDA_LIVE=1 ARTEMIDA_API_KEY=artm_live_… uv run pytest -m artemida_live tests/live/test_artemida_chain_live.py -q``.
Без ``ARTEMIDA_LIVE=1`` — пропускается целиком.

ВНИМАНИЕ: тратит 2 ₽ (создаёт один trial-ключ) и отзывает его в ``finally``.
В отличие от ``test_artemida_live`` (проверяет контракт клиента), этот тест
прогоняет ИМЕННО серверную цепочку выдачи клиенту:
  create_trial → ArtemidaProvider.fetch_links (реальные vless/trojan)
             → rebrand_links (наш бренд MAX, full-tunnel, remark переписан)
и проверяет, что получившийся sub-документ валиден и переодет.
"""

from __future__ import annotations

import asyncio
import base64
import os

import pytest

from app.external.artemida_api import ArtemidaAPIError, ArtemidaClient
from app.services.providers.artemida import ArtemidaProvider
from app.services.subscription_rebrand import rebrand_links


ENV_FLAG = 'ARTEMIDA_LIVE'
ENV_KEY = 'ARTEMIDA_API_KEY'

pytestmark = [
    pytest.mark.artemida_live,
    pytest.mark.skipif(
        os.environ.get(ENV_FLAG) != '1' or not os.environ.get(ENV_KEY),
        reason=f'{ENV_FLAG}=1 и {ENV_KEY} не заданы — живой API и деньги не трогаем',
    ),
]


class _Sub:
    """Минимальная подписка для fetch_links (нужен только external_ref)."""

    def __init__(self, external_ref: str):
        self.id = 0
        self.external_ref = external_ref


async def _fetch_links_with_retry(provider, sub, *, attempts: int = 4, delay: float = 3.0) -> list[str]:
    """subscription-сервер у свежего ключа пару секунд недоступен — ретраим транзиент."""
    last: ArtemidaAPIError | None = None
    for attempt in range(attempts):
        try:
            return await provider.fetch_links(sub)
        except ArtemidaAPIError as error:
            last = error
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
    raise last  # type: ignore[misc]


async def test_serving_chain_rebrands_real_vendor_links() -> None:
    api_key = os.environ[ENV_KEY]
    idem = f'live-chain-{os.urandom(6).hex()}'
    async with ArtemidaClient(api_key=api_key) as client:
        key = await client.create_trial(idempotency_key=idem)
        assert key.id
        try:
            # Реальная серверная цепочка выдачи: provider → fetch_links → rebrand.
            provider = ArtemidaProvider(client_factory=lambda: ArtemidaClient(api_key=api_key))
            links = await _fetch_links_with_retry(provider, _Sub(key.id))
            assert isinstance(links, list) and links, 'вендор не вернул ссылок'

            doc = rebrand_links(links, title='MAX VPN', remark_prefix='MAX', support_url='https://t.me/MaxSupport2')

            # Тело — валидный base64 sub-документ из переодетых ссылок.
            decoded = base64.b64decode(doc.body).decode()
            lines = [ln for ln in decoded.strip().splitlines() if ln]
            assert len(lines) == len(links)
            assert all(ln.split('://', 1)[0] in ('vless', 'trojan', 'ss') for ln in lines)
            # remark переписан на наш бренд.
            assert all(ln.rsplit('#', 1)[-1].startswith('MAX ') for ln in lines)
            # Бренд в заголовках, whitelist-роутинга нет (full-tunnel).
            assert doc.headers['profile-title'] == 'MAX VPN'
            assert 'routing' not in {k.lower() for k in doc.headers}
        finally:
            try:
                await client.revoke_key(key.id, idempotency_key=f'{idem}-revoke')
            except ArtemidaAPIError:
                pass  # отзыв trial-ключа у вендора 404-ит — ключ сам протухнет за сутки
