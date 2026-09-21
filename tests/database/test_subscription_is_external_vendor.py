"""Shared predicate: is a subscription provisioned on an external paid vendor?

``Subscription.is_external_vendor`` is the single source of truth used by the
money-safety guards (paid-vendor renew/revoke, and the free-extension guards):
a subscription is external-vendor-backed when ``external_provider`` is set AND
is not our own ``'remnawave'`` panel. Extending or revoking such a subscription
costs money at, or must be pushed to, that vendor — so the guards must never
misclassify a plain remnawave subscription as external.
"""

from __future__ import annotations

from app.database.models import Subscription


def _sub(provider: str | None) -> Subscription:
    return Subscription(external_provider=provider)


def test_unset_provider_is_not_external_vendor():
    # A freshly-built subscription that was never provisioned on any vendor.
    assert Subscription().is_external_vendor is False


def test_none_provider_is_not_external_vendor():
    assert _sub(None).is_external_vendor is False


def test_remnawave_provider_is_not_external_vendor():
    # Our own panel is NOT an external paid vendor — the guards must leave it alone.
    assert _sub('remnawave').is_external_vendor is False


def test_artemida_provider_is_external_vendor():
    assert _sub('artemida').is_external_vendor is True


def test_empty_string_provider_is_not_external_vendor():
    # Defensive: an empty string is falsy, same as "not provisioned".
    assert _sub('').is_external_vendor is False
