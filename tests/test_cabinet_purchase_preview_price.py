"""Regression test: the tariff-selection preview (/purchase-options) must show
prices that match what POST /purchase-tariff actually charges.

Real incident (2026-09-10): a user with a 40%-off promo group ('ГОД40') looked
at the "Максимум" tariff's 12-month card and saw 2880 RUB — because
_build_tariff_response() re-derived its own discount math (tariff's own listed
annual price * (1 - group_pct)), completely bypassing PricingEngine. The actual
charge, computed via PricingEngine.calculate_tariff_purchase_price() at
checkout, was 3600 RUB — a 720 RUB bait-and-switch the customer would only
discover after clicking "Купить".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cabinet.routes.subscription_modules.purchase import _build_tariff_response
from app.config import settings
from app.services.pricing_engine import pricing_engine


@pytest.fixture(autouse=True)
def _max_mode(monkeypatch):
    # DISCOUNT_STACKING_MODE=max is what's actually live in production for this
    # incident — the bug only reproduces there (in 'multiply' mode the tariff's
    # own discount and the group discount compound together, a separate, older,
    # already-covered code path).
    monkeypatch.setattr(settings, 'DISCOUNT_STACKING_MODE', 'max', raising=False)


def _tariff(*, period_prices: dict[str, int], device_limit: int, tariff_id: int = 15) -> MagicMock:
    t = MagicMock()
    t.id = tariff_id
    t.name = 'Максимум'
    t.description = ''
    t.tier_level = 3
    t.traffic_limit_gb = 800
    t.device_limit = device_limit
    t.device_price_kopeks = None
    t.allowed_squads = []
    t.is_active = True
    t.custom_days_enabled = False
    t.price_per_day_kopeks = 0
    t.min_days = None
    t.max_days = None
    t.custom_traffic_enabled = False
    t.traffic_price_per_gb_kopeks = 0
    t.min_traffic_gb = None
    t.max_traffic_gb = None
    t.traffic_topup_enabled = False
    t.max_topup_traffic_gb = 0
    t.traffic_reset_mode = None
    t.is_daily = False
    t.daily_price_kopeks = 0
    t.period_prices = period_prices
    t.get_traffic_topup_packages = MagicMock(return_value={})
    return t


def _god40_user() -> MagicMock:
    promo_group = MagicMock()
    promo_group.name = 'Промокод ГОД40 (-40% на год)'
    promo_group.get_discount_percent.side_effect = lambda category, days: 40 if category == 'period' and days == 360 else 0
    user = MagicMock()
    user.promo_group = promo_group
    user.get_primary_promo_group.return_value = promo_group
    user.promo_offer_discount_percent = 0  # no personal one-shot offer active
    user.promo_offer_discount_expires_at = None
    return user


class TestPurchasePreviewMatchesRealCharge:
    @pytest.mark.asyncio
    async def test_new_maksimum_tariff_annual_price_matches_purchase_tariff_charge(self):
        """Reproduces the real incident: tariff id 15 'Максимум', 800GB/3 devices,
        period_prices identical to production (480000 for 360 days = 20% built-in
        off nominal 600000), user with a 40% ГОД40 group discount, no personal
        offer. The preview MUST show the same 12-month price PricingEngine would
        actually charge at checkout, not the old inline-multiply result."""
        tariff = _tariff(period_prices={'30': 50000, '90': 135000, '180': 255000, '360': 480000}, device_limit=3)
        user = _god40_user()
        db = AsyncMock()

        # What POST /purchase-tariff actually charges (the real, already-fixed
        # single source of truth) — computed independently here, not copied from
        # the route under test, to genuinely cross-check the two.
        real_charge = await pricing_engine.calculate_tariff_purchase_price(tariff, 360, user=user)

        response = await _build_tariff_response(db, tariff, user=user)
        annual_period = next(p for p in response['periods'] if p['days'] == 360)

        assert annual_period['price_kopeks'] == real_charge.final_total
        assert annual_period['price_kopeks'] == 360000  # 40% off nominal 600000, exactly
        assert annual_period['price_kopeks'] != 288000  # the old, buggy bait-and-switch value

        # The displayed "was" price and badge must be internally consistent with
        # the displayed "now" price (was * (1 - badge%) == now), not just correct
        # in isolation — this is what actually broke before (badge said -40%
        # against a struck price that, combined, implied a different discount).
        original = annual_period['original_price_kopeks']
        badge_pct = annual_period['discount_percent']
        reconstructed = round(original * (100 - badge_pct) / 100)
        assert abs(reconstructed - annual_period['price_kopeks']) <= 1  # integer rounding only

    @pytest.mark.asyncio
    async def test_old_maksimum_tariff_annual_price_matches_purchase_tariff_charge(self):
        """Same check for the OTHER live 'Максимум' variant (id 4, old, 25%
        built-in annual discount instead of 20%) — the two tariffs must each be
        internally consistent even though their own built-in discounts differ."""
        tariff = _tariff(
            period_prices={'30': 50000, '90': 135000, '180': 255000, '360': 450000}, device_limit=3, tariff_id=4
        )
        user = _god40_user()
        db = AsyncMock()

        real_charge = await pricing_engine.calculate_tariff_purchase_price(tariff, 360, user=user)
        response = await _build_tariff_response(db, tariff, user=user)
        annual_period = next(p for p in response['periods'] if p['days'] == 360)

        assert annual_period['price_kopeks'] == real_charge.final_total
        assert annual_period['price_kopeks'] == 360000  # 40% off the same nominal 600000

    @pytest.mark.asyncio
    async def test_no_promo_group_user_sees_tariffs_own_discount_honestly(self):
        """A user with NO promo group buying the same tariff must see the price
        PricingEngine would actually charge them too (the tariff's own built-in
        20% annual discount, previously invisible/unbadged in this preview since
        the old code only ever looked at the promo group's own percentage)."""
        tariff = _tariff(period_prices={'30': 50000, '90': 135000, '180': 255000, '360': 480000}, device_limit=3)
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_offer_discount_percent = 0
        user.promo_offer_discount_expires_at = None
        db = AsyncMock()

        real_charge = await pricing_engine.calculate_tariff_purchase_price(tariff, 360, user=user)
        response = await _build_tariff_response(db, tariff, user=user)
        annual_period = next(p for p in response['periods'] if p['days'] == 360)

        assert annual_period['price_kopeks'] == real_charge.final_total
        assert annual_period['price_kopeks'] == 480000  # tariff's own listed price, unchanged from today
