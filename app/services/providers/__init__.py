from __future__ import annotations

from app.services.providers.artemida import ArtemidaProvider


class _RemnawaveMarker:
    """Sentinel: remnawave tariffs keep the existing SubscriptionService body."""

    name = 'remnawave'


def get_provider(tariff):
    provider = getattr(tariff, 'provider', 'remnawave') or 'remnawave'
    if provider == 'artemida':
        return ArtemidaProvider()
    return _RemnawaveMarker()
