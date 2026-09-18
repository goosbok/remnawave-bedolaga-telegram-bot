from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Subscription


class SubscriptionProvider(Protocol):
    name: str

    async def provision(self, *, db: AsyncSession, subscription: Subscription, days: int) -> None:
        """Provision the vendor key for a subscription.

        Precondition: subscription.tariff and subscription.end_date must already be loaded/set.
        """
        ...

    async def update(
        self, *, db: AsyncSession, subscription: Subscription, days: int | None = None, devices: int | None = None
    ) -> None: ...
    async def revoke(self, *, db: AsyncSession, subscription: Subscription) -> None: ...
    async def sync_usage(self, *, db: AsyncSession, subscription: Subscription) -> None: ...
    def build_subscription_url(self, subscription: Subscription) -> str: ...
