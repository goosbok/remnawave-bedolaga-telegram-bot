from __future__ import annotations

from app.services.providers.artemida import ArtemidaProvider


class _RemnawaveMarker:
    """Sentinel: remnawave tariffs keep the existing SubscriptionService body."""

    name = 'remnawave'


_PROVIDERS = {'artemida': ArtemidaProvider}


def register_provider(name, factory):
    """For tests / future vendors."""
    _PROVIDERS[name] = factory


def get_provider_by_name(name):
    if not name or name == 'remnawave':
        return _RemnawaveMarker()
    factory = _PROVIDERS.get(name)
    return factory() if factory else None


def get_provider(tariff):
    return get_provider_by_name(getattr(tariff, 'provider', 'remnawave') or 'remnawave')
