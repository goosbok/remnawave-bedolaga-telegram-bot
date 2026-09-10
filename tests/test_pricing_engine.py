import itertools

import pytest

from app.services.pricing_engine import PricingEngine, RenewalPricing


def test_renewal_pricing_is_frozen():
    p = RenewalPricing(
        base_price=29000,
        servers_price=5000,
        traffic_price=0,
        devices_price=0,
        promo_group_discount=0,
        promo_offer_discount=0,
        final_total=34000,
        period_days=30,
        is_tariff_mode=False,
    )
    assert p.final_total == 34000
    with pytest.raises(AttributeError):
        p.final_total = 0


class TestApplyDiscount:
    def test_basic_discount(self):
        assert PricingEngine.apply_discount(10000, 20) == 8000

    def test_zero_discount(self):
        assert PricingEngine.apply_discount(10000, 0) == 10000

    def test_full_discount(self):
        assert PricingEngine.apply_discount(10000, 100) == 0

    def test_negative_clamped(self):
        assert PricingEngine.apply_discount(10000, -5) == 10000

    def test_over_100_clamped(self):
        assert PricingEngine.apply_discount(10000, 150) == 0

    def test_integer_floor_division(self):
        assert PricingEngine.apply_discount(99900, 30) == 69930


class TestStackedDiscounts:
    def test_group_then_offer(self):
        final, g_val, o_val = PricingEngine.apply_stacked_discounts(10000, 20, 10)
        assert final == 7200
        assert g_val == 2000
        assert o_val == 800

    def test_no_discounts(self):
        final, g_val, o_val = PricingEngine.apply_stacked_discounts(10000, 0, 0)
        assert final == 10000
        assert g_val == 0
        assert o_val == 0

    def test_only_offer(self):
        final, g_val, o_val = PricingEngine.apply_stacked_discounts(10000, 0, 15)
        assert final == 8500
        assert g_val == 0
        assert o_val == 1500

    def test_only_group(self):
        result, gd, od = PricingEngine.apply_stacked_discounts(10000, 20, 0)
        assert result == 8000
        assert gd == 2000
        assert od == 0

    def test_both_100_percent(self):
        result, gd, od = PricingEngine.apply_stacked_discounts(10000, 100, 100)
        assert result == 0
        assert gd == 10000
        assert od == 0  # offer discount on 0 is 0

    def test_default_mode_is_multiply(self, monkeypatch):
        """No mode passed => reads settings.get_discount_stacking_mode(), default 'multiply'."""
        from app.config import settings

        monkeypatch.setattr(settings, 'DISCOUNT_STACKING_MODE', 'multiply', raising=False)
        final, g_val, o_val = PricingEngine.apply_stacked_discounts(10000, 20, 10)
        assert (final, g_val, o_val) == (7200, 2000, 800)


class TestStackedDiscountsMaxMode:
    def test_group_wins(self):
        final, g_val, o_val = PricingEngine.apply_stacked_discounts(10000, 20, 10, mode='max')
        assert final == 8000
        assert g_val == 2000
        assert o_val == 0

    def test_offer_wins(self):
        final, g_val, o_val = PricingEngine.apply_stacked_discounts(10000, 10, 20, mode='max')
        assert final == 8000
        assert g_val == 0
        assert o_val == 2000

    def test_tie_offer_wins(self):
        """On a tie the offer wins, so it still gets marked as consumed by callers."""
        final, g_val, o_val = PricingEngine.apply_stacked_discounts(10000, 20, 20, mode='max')
        assert final == 8000
        assert g_val == 0
        assert o_val == 2000

    def test_only_group_unaffected_by_mode(self):
        final, g_val, o_val = PricingEngine.apply_stacked_discounts(10000, 20, 0, mode='max')
        assert final == 8000
        assert g_val == 2000
        assert o_val == 0

    def test_only_offer_unaffected_by_mode(self):
        final, g_val, o_val = PricingEngine.apply_stacked_discounts(10000, 0, 15, mode='max')
        assert final == 8500
        assert g_val == 0
        assert o_val == 1500

    def test_explicit_mode_overrides_settings(self, monkeypatch):
        """Passing mode= directly must win over whatever settings says."""
        from app.config import settings

        monkeypatch.setattr(settings, 'DISCOUNT_STACKING_MODE', 'multiply', raising=False)
        final, g_val, o_val = PricingEngine.apply_stacked_discounts(10000, 20, 10, mode='max')
        assert final == 8000  # NOT 7200 (multiply result) — explicit mode wins
        assert g_val == 2000
        assert o_val == 0


class TestCombineGroupAndOfferWithTariff:
    def test_tariff_wins(self):
        """Tariff's own price (8500) beats group (9000) and offer (9500) — tariff wins."""
        final, g, o, won, source = PricingEngine._combine_group_and_offer(
            10000, 9000, 5, mode='max', tariff_amount=8500
        )
        assert final == 8500
        assert g == 1500
        assert o == 0
        assert won is False
        assert source == 'tariff'

    def test_group_wins_over_tariff(self):
        """Group (7500) beats tariff (8500) and offer (9500) — group wins."""
        final, g, o, won, source = PricingEngine._combine_group_and_offer(
            10000, 7500, 5, mode='max', tariff_amount=8500
        )
        assert final == 7500
        assert g == 2500
        assert o == 0
        assert won is False
        assert source == 'group'

    def test_tie_between_tariff_and_group_favors_group(self):
        final, g, o, won, source = PricingEngine._combine_group_and_offer(
            10000, 8000, 0, mode='max', tariff_amount=8000
        )
        assert final == 8000
        assert source == 'group'

    def test_offer_beats_both_tariff_and_group(self):
        """Offer (30% of 10000=3000 discount, final 7000) beats tariff (8500) and group (9000)."""
        final, g, o, won, source = PricingEngine._combine_group_and_offer(
            10000, 9000, 30, mode='max', tariff_amount=8500
        )
        assert final == 7000
        assert g == 0
        assert o == 3000
        assert won is True
        assert source == 'offer'

    def test_no_tariff_amount_behaves_as_before(self):
        """tariff_amount=None (the default, and the only way every pre-existing caller
        invokes this method) must reproduce EXACTLY the pre-existing 2-way behavior,
        including the new 5th return value carrying the correct source label."""
        # Group-wins case (reproduces TestStackedDiscountsMaxMode.test_group_wins,
        # i.e. apply_stacked_discounts(10000, 20, 10, mode='max') internals).
        final, g, o, won, source = PricingEngine._combine_group_and_offer(10000, 8000, 10, mode='max')
        assert (final, g, o, won) == (8000, 2000, 0, False)
        assert source == 'group'

        # Offer-wins case (reproduces TestStackedDiscountsMaxMode.test_offer_wins,
        # i.e. apply_stacked_discounts(10000, 10, 20, mode='max') internals).
        final, g, o, won, source = PricingEngine._combine_group_and_offer(10000, 9000, 20, mode='max')
        assert (final, g, o, won) == (8000, 0, 2000, True)
        assert source == 'offer'

        # Explicit tariff_amount=None must be byte-for-byte identical to omitting it.
        explicit_none = PricingEngine._combine_group_and_offer(10000, 9000, 20, mode='max', tariff_amount=None)
        assert explicit_none == (8000, 0, 2000, True, 'offer')

    def test_multiply_mode_ignores_tariff_amount(self):
        """'multiply' mode must ignore tariff_amount entirely — same result with or without it."""
        without = PricingEngine._combine_group_and_offer(10000, 8000, 10, mode='multiply')
        with_tariff = PricingEngine._combine_group_and_offer(10000, 8000, 10, mode='multiply', tariff_amount=1)
        assert without[:4] == with_tariff[:4]  # final, group_val, offer_val, offer_won all identical
        assert without[4] == 'group'
        assert with_tariff[4] == 'group'


from unittest.mock import AsyncMock, MagicMock, patch


class TestPeriodDaysValidation:
    @pytest.mark.asyncio
    async def test_negative_period_days_raises(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        with pytest.raises(ValueError, match='Invalid period_days'):
            await engine.calculate_renewal_price(db, subscription, -1)

    @pytest.mark.asyncio
    async def test_zero_period_days_raises(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        with pytest.raises(ValueError, match='Invalid period_days'):
            await engine.calculate_renewal_price(db, subscription, 0)

    @pytest.mark.asyncio
    async def test_float_period_days_raises(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        with pytest.raises(ValueError, match='Invalid period_days'):
            await engine.calculate_renewal_price(db, subscription, 30.0)


_server_id_seq = itertools.count(1)


def _make_server(
    price_kopeks=5000, is_available=True, is_full=False, allowed_promo_groups=None, server_id=None, squad_uuid=None
):
    if server_id is None:
        server_id = next(_server_id_seq)
    server = MagicMock()
    server.id = server_id
    server.squad_uuid = squad_uuid
    server.price_kopeks = price_kopeks
    server.is_available = is_available
    server.is_full = is_full
    server.allowed_promo_groups = allowed_promo_groups or []
    return server


class TestCalculateServersPrice:
    @pytest.mark.asyncio
    async def test_available_server(self):
        engine = PricingEngine()
        db = AsyncMock()
        server = _make_server(price_kopeks=5000, squad_uuid='uuid-1')
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]):
            total, details = await engine._calculate_servers_price(['uuid-1'], db, promo_group_id=None)
        assert total == 5000
        assert len(details) == 1
        assert details[0]['price'] == 5000

    @pytest.mark.asyncio
    async def test_unavailable_server_uses_real_price(self):
        engine = PricingEngine()
        db = AsyncMock()
        server = _make_server(price_kopeks=7000, is_available=False, squad_uuid='uuid-1')
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]):
            total, details = await engine._calculate_servers_price(['uuid-1'], db, promo_group_id=None)
        assert total == 7000  # NOT 0!
        assert details[0]['status'] == 'unavailable'

    @pytest.mark.asyncio
    async def test_full_server_uses_real_price(self):
        engine = PricingEngine()
        db = AsyncMock()
        server = _make_server(price_kopeks=3000, is_full=True, squad_uuid='uuid-1')
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]):
            total, details = await engine._calculate_servers_price(['uuid-1'], db, promo_group_id=None)
        assert total == 3000  # NOT 0!

    @pytest.mark.asyncio
    async def test_server_not_found_zero_price(self):
        engine = PricingEngine()
        db = AsyncMock()
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[]):
            total, details = await engine._calculate_servers_price(['uuid-orphan'], db, promo_group_id=None)
        assert total == 0
        assert details[0]['status'] == 'not_found'

    @pytest.mark.asyncio
    async def test_multiple_servers(self):
        engine = PricingEngine()
        db = AsyncMock()
        s1 = _make_server(price_kopeks=5000, squad_uuid='uuid-1')
        s2 = _make_server(price_kopeks=3000, is_available=False, squad_uuid='uuid-2')
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[s1, s2]):
            total, details = await engine._calculate_servers_price(['uuid-1', 'uuid-2'], db, promo_group_id=None)
        assert total == 8000

    @pytest.mark.asyncio
    async def test_server_ids_and_prices_alignment_with_not_found(self):
        """Verify server_ids and servers_individual_prices have same length when some servers are not found."""
        engine = PricingEngine()
        db = AsyncMock()
        s1 = _make_server(price_kopeks=5000, server_id=10, squad_uuid='uuid-1')
        s3 = _make_server(price_kopeks=3000, server_id=30, squad_uuid='uuid-3')
        # uuid-orphan not in batch result — should be excluded from BOTH lists
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[s1, s3]):
            total, details = await engine._calculate_servers_price(
                ['uuid-1', 'uuid-orphan', 'uuid-3'], db, promo_group_id=None
            )
        assert total == 8000  # 5000 + 0 + 3000
        assert len(details) == 3
        # Verify id fields
        assert details[0]['id'] == 10
        assert details[1]['id'] is None
        assert details[2]['id'] == 30

    @pytest.mark.asyncio
    async def test_db_exception_path(self):
        """Verify batch DB exception returns price=0 and status=error for all UUIDs."""
        engine = PricingEngine()
        db = AsyncMock()
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', side_effect=RuntimeError('DB error')):
            total, details = await engine._calculate_servers_price(['uuid-1'], db, promo_group_id=None)
        assert total == 0
        assert details[0]['status'] == 'error'
        assert details[0]['id'] is None

    @pytest.mark.asyncio
    async def test_empty_uuids_returns_empty(self):
        """Verify empty UUIDs list returns 0 total and empty details."""
        engine = PricingEngine()
        db = AsyncMock()
        total, details = await engine._calculate_servers_price([], db, promo_group_id=None)
        assert total == 0
        assert details == []


class TestCalculateTrafficPrice:
    def test_base_only(self):
        engine = PricingEngine()
        with patch('app.services.pricing_engine.settings') as ms:
            ms.get_traffic_price.side_effect = lambda gb: {25: 3000, 50: 5000}.get(gb, 0)
            price = engine._calculate_traffic_price(traffic_limit_gb=25, purchased_traffic_gb=0)
        assert price == 3000

    def test_purchased_separated(self):
        engine = PricingEngine()
        with patch('app.services.pricing_engine.settings') as ms:
            ms.get_traffic_price.side_effect = lambda gb: {25: 3000, 100: 8000, 125: 12000}.get(gb, 0)
            price = engine._calculate_traffic_price(traffic_limit_gb=125, purchased_traffic_gb=100)
        assert price == 11000  # NOT 12000

    def test_unlimited_traffic_has_price(self):
        engine = PricingEngine()
        with patch('app.services.pricing_engine.settings') as ms:
            ms.get_traffic_price.side_effect = lambda gb: {0: 20000, 5: 2000}.get(gb, 0)
            price = engine._calculate_traffic_price(traffic_limit_gb=0, purchased_traffic_gb=0)
        assert price == 20000  # 0 GB = unlimited, charged at unlimited tier

    def test_unlimited_traffic_ignores_purchased(self):
        engine = PricingEngine()
        with patch('app.services.pricing_engine.settings') as ms:
            ms.get_traffic_price.side_effect = lambda gb: {0: 20000, 50: 5000}.get(gb, 0)
            price = engine._calculate_traffic_price(traffic_limit_gb=0, purchased_traffic_gb=50)
        assert price == 20000  # unlimited tier, purchased ignored

    def test_purchased_exceeds_total(self):
        engine = PricingEngine()
        with patch('app.services.pricing_engine.settings') as ms:
            ms.get_traffic_price.side_effect = lambda gb: {0: 0, 100: 8000}.get(gb, 0)
            price = engine._calculate_traffic_price(traffic_limit_gb=80, purchased_traffic_gb=100)
        assert price == 8000  # base_gb clamped to 0


class TestCalculateRenewalPriceTariffMode:
    @pytest.mark.asyncio
    async def test_tariff_basic(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 2
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 19000}
        subscription.tariff.device_limit = 2
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 2
        subscription.device_limit = 2
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 50
        subscription.purchased_traffic_gb = 0
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_offer_discount_percent = 0
        user.promo_offer_expires_at = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.is_tariff_mode is True
        assert result.final_total == 19000

    @pytest.mark.asyncio
    async def test_tariff_extra_devices(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 2
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 19000}
        subscription.tariff.device_limit = 2
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 2
        subscription.device_limit = 4
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 50
        subscription.purchased_traffic_gb = 0
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_offer_discount_percent = 0
        user.promo_offer_expires_at = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.devices_price == 10000
        assert result.final_total == 29000

    @pytest.mark.asyncio
    async def test_tariff_device_price_from_tariff(self):
        """When tariff has device_price_kopeks set, use it instead of settings."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 2
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 10000}
        subscription.tariff.device_limit = 2
        subscription.tariff.device_price_kopeks = 3000  # tariff-specific price
        subscription.tariff.id = 2
        subscription.device_limit = 4  # 2 extra devices
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000  # should NOT be used
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.devices_price == 6000  # 2 extra × 3000 (tariff price)
        assert result.final_total == 16000  # 10000 + 6000

    @pytest.mark.asyncio
    async def test_tariff_with_discounts(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 1
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 20000}
        subscription.tariff.device_limit = 1
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 1
        subscription.device_limit = 1
        promo_group = MagicMock()
        promo_group.get_discount_percent.return_value = 10
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=5),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.base_price == 18000  # 20000 discounted by 10%
        assert result.promo_group_discount == 2000
        # After group: 18000, then 5% off 18000 = 900
        assert result.promo_offer_discount == 900
        assert result.final_total == 17100

    @pytest.mark.asyncio
    async def test_tariff_with_discounts_max_mode_group_wins(self):
        """Group discount (10% = 2000) beats offer (5% of raw 20000 = 1000) — offer unused."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 1
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 20000}
        subscription.tariff.device_limit = 1
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 1
        subscription.device_limit = 1
        promo_group = MagicMock()
        promo_group.get_discount_percent.return_value = 10
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=5),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.base_price == 18000  # 20000 discounted by 10% — group applies
        assert result.promo_group_discount == 2000
        assert result.promo_offer_discount == 0  # offer NOT consumed — it wasn't the bigger discount
        assert result.final_total == 18000

    @pytest.mark.asyncio
    async def test_tariff_with_discounts_max_mode_offer_wins(self):
        """Offer (30% of raw 20000 = 6000) beats group (10% = 2000) — group unused."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 1
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 20000}
        subscription.tariff.device_limit = 1
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 1
        subscription.device_limit = 1
        promo_group = MagicMock()
        promo_group.get_discount_percent.return_value = 10
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=30),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.base_price == 20000  # raw — group discount not applied
        assert result.promo_group_discount == 0
        assert result.promo_offer_discount == 6000
        assert result.final_total == 14000

    @pytest.mark.asyncio
    async def test_tariff_with_discounts_max_mode_tie(self):
        """Reproduces the real production case: two 25% discounts. Tie => offer wins (consumed),
        customer pays 25% off (15000), not 43.75% off (11250) like the old multiply behavior."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 1
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 20000}
        subscription.tariff.device_limit = 1
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 1
        subscription.device_limit = 1
        promo_group = MagicMock()
        promo_group.get_discount_percent.return_value = 25
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=25),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.promo_group_discount == 0
        assert result.promo_offer_discount == 5000  # consumed — tie goes to the offer
        assert result.final_total == 15000

    @pytest.mark.asyncio
    async def test_tariff_with_discounts_max_mode_group_wins_with_devices(self):
        """Same as group_wins, but with a non-zero devices component — confirms devices_price
        is NOT reset when the group discount wins (it correctly stays group-discounted)."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 1
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 20000}
        subscription.tariff.device_limit = 1
        subscription.tariff.device_price_kopeks = 1000
        subscription.tariff.id = 1
        subscription.device_limit = 3  # 2 extra devices
        promo_group = MagicMock()
        promo_group.get_discount_percent.return_value = 10
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=5),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000  # should NOT be used — tariff.device_price_kopeks wins
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        # raw: base 20000 + devices 2*1000=2000 = 22000. group 10%: base->18000, devices->1800.
        # group_discount_value = 2200. offer 5% of raw 22000 = 1100. 1100 < 2200 -> group wins.
        assert result.base_price == 18000
        assert result.devices_price == 1800  # group-discounted, NOT reset
        assert result.promo_group_discount == 2200
        assert result.promo_offer_discount == 0
        assert result.final_total == 19800

    @pytest.mark.asyncio
    async def test_tariff_with_discounts_max_mode_offer_wins_with_devices(self):
        """Same as offer_wins, but with a non-zero devices component — confirms devices_price
        IS reset to raw when the offer wins (this is the exact regression the code-quality
        review flagged: a partial reset that only touched base_price would silently
        undercharge or overcharge the devices portion)."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 1
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 20000}
        subscription.tariff.device_limit = 1
        subscription.tariff.device_price_kopeks = 1000
        subscription.tariff.id = 1
        subscription.device_limit = 3  # 2 extra devices
        promo_group = MagicMock()
        promo_group.get_discount_percent.return_value = 10
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=30),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000  # should NOT be used — tariff.device_price_kopeks wins
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        # raw: base 20000 + devices 2000 = 22000. offer 30% of raw 22000 = 6600.
        # group_discount_value = 2200 (see group-wins case above). 6600 >= 2200 -> offer wins.
        assert result.base_price == 20000  # reset to raw
        assert result.devices_price == 2000  # reset to raw — this is what the review flagged as untested
        assert result.promo_group_discount == 0
        assert result.promo_offer_discount == 6600
        assert result.final_total == 15400

    @pytest.mark.asyncio
    async def test_tariff_offer_wins_breakdown_percentages_reset(self):
        """When the offer wins, breakdown['group_discount_pct'] must show 0%, not the
        stale group percentages — otherwise a purchase confirmation screen would display
        a group discount that wasn't actually the one applied."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 1
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 20000}
        subscription.tariff.device_limit = 1
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 1
        subscription.device_limit = 1
        promo_group = MagicMock()
        promo_group.get_discount_percent.return_value = 10
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=30),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.promo_group_discount == 0
        assert result.breakdown['group_discount_pct'] == {'period': 0, 'devices': 0}

    @pytest.mark.asyncio
    async def test_tariff_missing_period_returns_zero_base(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 1
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 19000}
        subscription.tariff.device_limit = 1
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 1
        subscription.tariff.is_daily = False
        subscription.tariff.can_purchase_custom_days.return_value = False
        subscription.device_limit = 1
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            result = await engine.calculate_renewal_price(db, subscription, 60, user=user)
        assert result.base_price == 0
        assert result.final_total == 0

    @pytest.mark.asyncio
    async def test_tariff_device_limit_below_tariff_included(self):
        """When subscription device_limit < tariff device_limit, extra_devices is 0 (not negative)."""
        engine = PricingEngine()
        db = AsyncMock()
        tariff = MagicMock()
        tariff.id = 1
        tariff.period_prices = {'30': 10000}
        tariff.device_price_kopeks = 5000
        tariff.device_limit = 5
        sub = MagicMock()
        sub.tariff_id = 1
        sub.tariff = tariff
        sub.device_limit = 2  # less than tariff's 5
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None

        with patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0):
            result = await engine.calculate_renewal_price(db, sub, 30, user=user)

        assert result.devices_price == 0
        assert result.final_total == 10000
        assert result.breakdown.get('extra_devices') == 0

    @pytest.mark.asyncio
    async def test_tariff_user_none(self):
        """When user=None, no discounts are applied."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 1
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 20000}
        subscription.tariff.device_limit = 1
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 1
        subscription.device_limit = 1
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            result = await engine.calculate_renewal_price(db, subscription, 30, user=None)
        assert result.final_total == 20000
        assert result.promo_group_discount == 0
        assert result.promo_offer_discount == 0

    @pytest.mark.asyncio
    async def test_tariff_period_discount_beats_group_when_group_smaller(self):
        """Tariff's own built-in discount (20%) beats a smaller group discount (10%) —
        the group discount, even though configured, should NOT apply; tariff wins."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 1
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 35000, '360': 336000}  # 336000 = 20% off 12*35000=420000
        subscription.tariff.device_limit = 1
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 1
        subscription.tariff.is_daily = False
        subscription.tariff.can_purchase_custom_days.return_value = False
        subscription.device_limit = 1
        promo_group = MagicMock()
        promo_group.get_discount_percent.return_value = 10  # smaller than tariff's own 20%
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 360, user=user)
        assert result.base_price == 336000  # tariff's own price, unchanged — group's 10% did NOT apply
        assert result.promo_group_discount == 420000 - 336000  # 84000 — but attributed via tariff, not group
        assert result.breakdown['group_discount_pct']['period'] == 0  # group did NOT win, must show 0
        assert result.breakdown['base_discount_source'] == 'tariff'
        assert result.breakdown['tariff_period_discount_pct'] == 20
        assert result.final_total == 336000

    @pytest.mark.asyncio
    async def test_group_discount_beats_tariff_god40_new_tariff_case(self):
        """The real motivating case: a 'ГОД40' style promo group offering 40% beats the
        tariff's own smaller 20% built-in discount. Reproduces tariff id 14 'Активный'
        exactly (nominal 420000 kopeks = 4200 rub, tariff price 336000 = 3360 rub)."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 14
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 35000, '360': 336000}
        subscription.tariff.device_limit = 2
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 14
        subscription.tariff.is_daily = False
        subscription.tariff.can_purchase_custom_days.return_value = False
        subscription.device_limit = 2
        promo_group = MagicMock()
        promo_group.get_discount_percent.return_value = 40
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 360, user=user)
        assert result.final_total == 252000  # 420000 * 0.60 = 40% off nominal, exactly
        assert result.base_price == 252000
        assert result.breakdown['base_discount_source'] == 'group'
        assert result.breakdown['group_discount_pct']['period'] == 40
        assert result.breakdown['tariff_period_discount_pct'] == 20

    @pytest.mark.asyncio
    async def test_group_discount_beats_tariff_god40_old_tariff_case(self):
        """Same promo group (40%), but an 'old' tariff with a bigger built-in discount
        (25%, tariff id 5 'Команда (старый)': nominal 1800000, tariff price 1350000).
        Group (40%) still wins since 40% > 25% — this is the exact case that used to
        give 43.75% instead of 40% before this feature (multiplicative compounding)."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 5
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 150000, '360': 1350000}  # 25% off 12*150000=1800000
        subscription.tariff.device_limit = 20
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 5
        subscription.tariff.is_daily = False
        subscription.tariff.can_purchase_custom_days.return_value = False
        subscription.device_limit = 20
        promo_group = MagicMock()
        promo_group.get_discount_percent.return_value = 40
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 360, user=user)
        assert result.final_total == 1080000  # 1800000 * 0.60 = exactly 40% off nominal
        assert result.breakdown['base_discount_source'] == 'group'
        assert result.breakdown['tariff_period_discount_pct'] == 25

    @pytest.mark.asyncio
    async def test_multiply_mode_unaffected_by_this_feature(self):
        """'multiply' mode must remain EXACTLY as before — tariff's own discount stays
        baked into base_price and the group discount compounds with it, unchanged."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 5
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 150000, '360': 1350000}
        subscription.tariff.device_limit = 20
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 5
        subscription.tariff.is_daily = False
        subscription.tariff.can_purchase_custom_days.return_value = False
        subscription.device_limit = 20
        promo_group = MagicMock()
        promo_group.get_discount_percent.return_value = 25  # the CURRENT live group_pct value, pre-fix
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            ms.get_discount_stacking_mode.return_value = 'multiply'
            result = await engine.calculate_renewal_price(db, subscription, 360, user=user)
        # 1350000 * 0.75 (25% group, compounding with the already-baked-in tariff price) = 1012500
        assert result.final_total == 1012500
        assert result.breakdown['base_discount_source'] == 'group'

    @pytest.mark.asyncio
    async def test_offer_still_wins_over_both_tariff_and_group(self):
        """Sanity check the three-way system still lets a big personal offer win outright,
        exactly like before this task (regression guard for the already-shipped behavior)."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 14
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 35000, '360': 336000}
        subscription.tariff.device_limit = 2
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 14
        subscription.tariff.is_daily = False
        subscription.tariff.can_purchase_custom_days.return_value = False
        subscription.device_limit = 2
        promo_group = MagicMock()
        promo_group.get_discount_percent.return_value = 40
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=60),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 360, user=user)
        assert result.final_total == 168000  # 420000 * 0.40 (60% off beats 40% group)
        assert result.breakdown['base_discount_source'] == 'offer'
        assert result.breakdown['group_discount_pct']['period'] == 0
        assert result.breakdown['tariff_period_discount_pct'] == 20  # informational, unaffected by who won
        assert result.promo_offer_discount == 420000 - 168000

    @pytest.mark.asyncio
    async def test_tariff_own_discount_wins_when_user_has_no_promo_group(self):
        """The most common real-world 'max' mode case: a user with no promo group at
        all, on a tariff with its own built-in annual discount. The tariff's own price
        must apply (as always), reported with base_discount_source='tariff', and
        group_discount_pct must show 0% (there's no group to attribute it to) even
        though promo_group_discount carries the tariff's own discount value — this
        is the documented, intentional 'total non-offer discount' semantics of that
        field, not a bug."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 14
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 35000, '360': 336000}  # 20% built-in
        subscription.tariff.device_limit = 2
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 14
        subscription.tariff.is_daily = False
        subscription.tariff.can_purchase_custom_days.return_value = False
        subscription.device_limit = 2
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None  # no promo group at all
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 360, user=user)
        assert result.final_total == 336000  # tariff's own price — unchanged from today's behavior
        assert result.base_price == 336000
        assert result.breakdown['base_discount_source'] == 'tariff'
        assert result.breakdown['group_discount_pct']['period'] == 0
        assert result.breakdown['tariff_period_discount_pct'] == 20
        # promo_group_discount carries the tariff's own discount value by design
        # (see RenewalPricing.promo_group_discount's docstring/comment) — 420000-336000.
        assert result.promo_group_discount == 84000

    @pytest.mark.asyncio
    async def test_max_mode_with_custom_days_period(self):
        """Custom-days purchases (not a standard 30/90/180/360 period) must not crash
        under the new 3-way logic, even though `months` is only an approximation
        (max(1, round(days/30))) for such periods."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = 14
        subscription.tariff = MagicMock()
        subscription.tariff.period_prices = {'30': 35000, '360': 336000}
        subscription.tariff.device_limit = 2
        subscription.tariff.device_price_kopeks = None
        subscription.tariff.id = 14
        subscription.tariff.is_daily = False
        subscription.tariff.can_purchase_custom_days.return_value = True
        subscription.tariff.get_price_for_custom_days.return_value = 60000  # 200 days, tariff's own custom price
        subscription.device_limit = 2
        promo_group = MagicMock()
        promo_group.get_discount_percent.return_value = 40
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
        ):
            ms.PRICE_PER_DEVICE = 5000
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 200, user=user)
        # No crash, and a well-formed result. Observed values (months=7 from
        # calculate_months_from_days(200), nominal_base=35000*7=245000): the tariff's
        # own custom-days price (60000) beats even the 40%-off-nominal group price
        # (147000), so the tariff wins outright. The `months` approximation (200 days
        # rounds to 7 months, not ~6.67) inflates nominal_base relative to the actual
        # period, which widens the tariff's apparent discount pct below — a
        # pre-existing characteristic of calculate_months_from_days, not a bug
        # introduced by this feature.
        assert result.final_total > 0
        assert result.breakdown['base_discount_source'] in ('tariff', 'group', 'offer')
        assert result.final_total == 60000
        assert result.base_price == 60000
        assert result.breakdown['base_discount_source'] == 'tariff'
        assert result.breakdown['group_discount_pct']['period'] == 0
        assert result.breakdown['tariff_period_discount_pct'] == 76
        assert result.promo_group_discount == 185000


class TestCalculateRenewalPriceClassicMode:
    @pytest.mark.asyncio
    async def test_classic_all_components(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = ['uuid-1']
        subscription.traffic_limit_gb = 50
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 2
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_group_id = None
        user.promo_offer_discount_percent = 0
        user.promo_offer_expires_at = None
        server = _make_server(price_kopeks=5000, squad_uuid='uuid-1')
        with (
            patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]),
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 29000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {30: 29000}),
        ):
            ms.get_traffic_price.return_value = 3000
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 2
            ms.is_traffic_fixed.return_value = False
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.is_tariff_mode is False
        assert result.base_price == 29000
        assert result.servers_price == 5000
        assert result.traffic_price == 3000
        assert result.final_total == 37000

    @pytest.mark.asyncio
    async def test_classic_with_discounts(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 0
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 2
        promo_group = MagicMock()
        promo_group.id = 1
        promo_group.get_discount_percent.return_value = 20
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        user.promo_group_id = 1
        user.promo_offer_discount_percent = 10
        user.promo_offer_expires_at = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=10),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {30: 10000}),
        ):
            ms.get_traffic_price.return_value = 0
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 2
            ms.is_traffic_fixed.return_value = False
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.final_total == 7200
        assert result.promo_group_discount == 2000
        assert result.promo_offer_discount == 800

    @pytest.mark.asyncio
    async def test_classic_with_discounts_max_mode(self):
        """Same inputs as test_classic_with_discounts (group 20% / offer 10% on base 10000),
        but in max mode: group (2000) beats offer (10% of raw 10000 = 1000) — offer unused."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 0
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 2
        promo_group = MagicMock()
        promo_group.id = 1
        promo_group.get_discount_percent.return_value = 20
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        user.promo_group_id = 1
        user.promo_offer_discount_percent = 10
        user.promo_offer_expires_at = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=10),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {30: 10000}),
        ):
            ms.get_traffic_price.return_value = 0
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 2
            ms.is_traffic_fixed.return_value = False
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.final_total == 8000
        assert result.promo_group_discount == 2000
        assert result.promo_offer_discount == 0

    @pytest.mark.asyncio
    async def test_classic_with_discounts_max_mode_offer_wins(self):
        """Offer wins outright (max mode) with non-zero servers/traffic/devices components —
        confirms ALL FOUR components (not just base_price) reset to raw, undiscounted values.
        This is the exact scenario the reset logic exists for; a partial reset (e.g. forgetting
        devices_price or dropping the *months multiplier on servers/traffic) would silently
        undercharge or overcharge part of the bill without this test catching it."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = ['uuid-1']
        subscription.traffic_limit_gb = 50
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 4  # 2 extra devices beyond DEFAULT_DEVICE_LIMIT=2
        promo_group = MagicMock()
        promo_group.id = 1
        promo_group.get_discount_percent.return_value = 10
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        user.promo_group_id = 1
        user.promo_offer_discount_percent = 30
        user.promo_offer_expires_at = None
        server = _make_server(price_kopeks=5000, squad_uuid='uuid-1')
        with (
            patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]),
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=30),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {30: 10000}),
        ):
            ms.get_traffic_price.return_value = 3000
            ms.PRICE_PER_DEVICE = 1000
            ms.DEFAULT_DEVICE_LIMIT = 2
            ms.is_traffic_fixed.return_value = False
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        # raw components (1 month): base 10000, servers 5000, traffic 3000, devices 2*1000=2000.
        # raw_subtotal = 20000. group 10% on the discounted subtotal (18000) -> group_discount_value=2000.
        # offer 30% of raw 20000 = 6000. 6000 >= 2000 -> offer wins, all four reset to raw.
        assert result.base_price == 10000
        assert result.servers_price == 5000
        assert result.traffic_price == 3000
        assert result.devices_price == 2000
        assert result.promo_group_discount == 0
        assert result.promo_offer_discount == 6000
        assert result.final_total == 14000

    @pytest.mark.asyncio
    async def test_classic_offer_wins_breakdown_percentages_reset(self):
        """Regression test: when the offer wins, the breakdown's per-category percentages
        must also reset to 0, not just the aggregate prices. Before this fix, stale
        non-zero percentages here made classic_pricing_to_purchase_details() re-derive
        a phantom discount that failed validate_pricing_calculation() and broke real
        classic-mode purchases whenever the offer won with a non-zero servers/traffic/
        devices group discount."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = ['uuid-1']
        subscription.traffic_limit_gb = 50
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 4
        promo_group = MagicMock()
        promo_group.id = 1
        promo_group.get_discount_percent.return_value = 10
        user = MagicMock()
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        user.promo_group_id = 1
        user.promo_offer_discount_percent = 30
        user.promo_offer_expires_at = None
        server = _make_server(price_kopeks=5000, squad_uuid='uuid-1')
        with (
            patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]),
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=30),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {30: 10000}),
        ):
            ms.get_traffic_price.return_value = 3000
            ms.PRICE_PER_DEVICE = 1000
            ms.DEFAULT_DEVICE_LIMIT = 2
            ms.is_traffic_fixed.return_value = False
            ms.get_discount_stacking_mode.return_value = 'max'
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)

        # Offer wins (same numbers as test_classic_with_discounts_max_mode_offer_wins).
        assert result.promo_group_discount == 0
        assert result.promo_offer_discount == 6000

        # THE BUG: breakdown percentages must be 0, not the stale group percentages.
        assert result.breakdown['group_discount_pct'] == {'period': 0, 'servers': 0, 'traffic': 0, 'devices': 0}
        assert result.breakdown['servers_individual_prices'] == [5000]  # raw, not group-discounted

        # THE ACTUAL PRODUCTION FAILURE: classic_pricing_to_purchase_details() must produce
        # internally-consistent numbers that pass validate_pricing_calculation(), i.e. this
        # must not raise and must reconstruct the same final_total.
        from app.utils.pricing_utils import validate_pricing_calculation

        details = PricingEngine.classic_pricing_to_purchase_details(result)
        assert details['traffic_discount_total'] == 0
        assert details['servers_discount_total'] == 0
        assert details['devices_discount_total'] == 0
        months = details['months_in_period']
        # Mirrors the reconstruction in SubscriptionPurchaseService.calculate_pricing().
        reconstructed_monthly = (
            (details['traffic_price_per_month'] - details['traffic_discount_total'] // max(1, months))
            + (details['servers_price_per_month'] - details['servers_discount_total'] // max(1, months))
            + (details['devices_price_per_month'] - details['devices_discount_total'] // max(1, months))
        )
        discounted_total = result.final_total + result.promo_offer_discount
        assert validate_pricing_calculation(details['base_price'], reconstructed_monthly, months, discounted_total) is True

    @pytest.mark.asyncio
    async def test_classic_fallback_to_period_prices(self):
        """When CLASSIC_PERIOD_PRICES has no entry, falls back to PERIOD_PRICES."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 0
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 1
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_group_id = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {30: 99000}),
        ):
            ms.get_traffic_price.return_value = 0
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 1
            ms.is_traffic_fixed.return_value = False
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.base_price == 99000
        assert result.final_total == 99000

    @pytest.mark.asyncio
    async def test_classic_extra_devices(self):
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 0
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 5
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_group_id = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.get_traffic_price.return_value = 0
            ms.PRICE_PER_DEVICE = 3000
            ms.DEFAULT_DEVICE_LIMIT = 2
            ms.is_traffic_fixed.return_value = False
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        # 5 - 2 = 3 extra devices * 3000 = 9000
        assert result.devices_price == 9000
        assert result.final_total == 19000

    @pytest.mark.asyncio
    async def test_classic_breakdown_server_ids_and_prices_alignment(self):
        """Verify server_ids and servers_individual_prices have same length when orphaned UUIDs present."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = ['uuid-found', 'uuid-orphan', 'uuid-found2']
        subscription.traffic_limit_gb = 0
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 1
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_group_id = None
        s1 = _make_server(price_kopeks=5000, server_id=10, squad_uuid='uuid-found')
        s3 = _make_server(price_kopeks=3000, server_id=30, squad_uuid='uuid-found2')
        with (
            patch(
                'app.services.pricing_engine.get_server_squads_by_uuids',
                return_value=[s1, s3],
            ),
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.get_traffic_price.return_value = 0
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 1
            ms.is_traffic_fixed.return_value = False
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        # Verify breakdown alignment — both lists must have same length
        ids = result.breakdown['server_ids']
        prices = result.breakdown['servers_individual_prices']
        assert len(ids) == len(prices), f'server_ids({len(ids)}) != prices({len(prices)})'
        assert ids == [10, 30]
        assert prices == [5000, 3000]
        # Total servers_price includes only found servers
        assert result.servers_price == 8000

    @pytest.mark.asyncio
    async def test_classic_fixed_traffic_ignores_subscription_values(self):
        """When is_traffic_fixed() is True, use fixed limit and zero purchased."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 999  # should be ignored
        subscription.purchased_traffic_gb = 500  # should be ignored
        subscription.device_limit = 1
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_group_id = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.is_traffic_fixed.return_value = True
            ms.get_fixed_traffic_limit.return_value = 50
            ms.get_traffic_price.side_effect = lambda gb: {50: 4000}.get(gb, 0)
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 1
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.traffic_price == 4000
        assert result.breakdown['purchased_traffic_gb'] == 0

    @pytest.mark.asyncio
    async def test_classic_default_traffic_limit_when_none(self):
        """When subscription.traffic_limit_gb is None, use DEFAULT_TRAFFIC_LIMIT_GB."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = []
        subscription.traffic_limit_gb = None  # should fallback to default
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 1
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_group_id = None
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.is_traffic_fixed.return_value = False
            ms.DEFAULT_TRAFFIC_LIMIT_GB = 50
            ms.get_traffic_price.side_effect = lambda gb: {50: 4000}.get(gb, 0)
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 1
            result = await engine.calculate_renewal_price(db, subscription, 30, user=user)
        assert result.traffic_price == 4000

    @pytest.mark.asyncio
    async def test_classic_multi_month_period(self):
        """90-day period multiplies monthly prices by 3."""
        engine = PricingEngine()
        db = AsyncMock()
        sub = MagicMock()
        sub.tariff_id = None
        sub.tariff = None
        sub.connected_squads = ['uuid-s1']
        sub.traffic_limit_gb = 50
        sub.purchased_traffic_gb = 0
        sub.device_limit = 1
        user = MagicMock()
        user.promo_group = None
        user.get_primary_promo_group.return_value = None
        user.promo_group_id = None

        server = _make_server(price_kopeks=3000, squad_uuid='uuid-s1')

        with (
            patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]),
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {90: 27000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.DEFAULT_DEVICE_LIMIT = 1
            ms.PRICE_PER_DEVICE = 5000
            ms.get_traffic_price.return_value = 2000
            ms.is_traffic_fixed.return_value = False
            ms.DEFAULT_TRAFFIC_LIMIT_GB = 50

            result = await engine.calculate_renewal_price(db, sub, 90, user=user)

        assert result.period_days == 90
        assert result.base_price == 27000
        # Servers and traffic are monthly x 3 months
        assert result.servers_price == 3000 * 3
        assert result.traffic_price == 2000 * 3
        assert result.devices_price == 0  # no extra devices
        assert result.final_total == 27000 + 9000 + 6000

    @pytest.mark.asyncio
    async def test_classic_per_category_different_discounts(self):
        """Different discount percents per category (period=10%, servers=20%, traffic=30%, devices=0%)."""
        engine = PricingEngine()
        db = AsyncMock()
        sub = MagicMock()
        sub.tariff_id = None
        sub.tariff = None
        sub.connected_squads = ['uuid-s1']
        sub.traffic_limit_gb = 100
        sub.purchased_traffic_gb = 0
        sub.device_limit = 3  # 2 extra devices

        user = MagicMock()
        promo_group = MagicMock()

        def discount_by_category(category, period_days):
            return {'period': 10, 'servers': 20, 'traffic': 30, 'devices': 0}[category]

        promo_group.get_discount_percent = MagicMock(side_effect=discount_by_category)
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        user.promo_group_id = 1

        server = _make_server(price_kopeks=6000, squad_uuid='uuid-s1')

        with (
            patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]),
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.DEFAULT_DEVICE_LIMIT = 1
            ms.PRICE_PER_DEVICE = 4000
            ms.get_traffic_price.return_value = 5000
            ms.is_traffic_fixed.return_value = False
            ms.DEFAULT_TRAFFIC_LIMIT_GB = 100

            result = await engine.calculate_renewal_price(db, sub, 30, user=user)

        # period: 10000 * 10% = 1000 discount -> 9000
        assert result.base_price == 9000
        # servers: 6000 * 20% = 1200 discount -> 4800 per month x 1
        assert result.servers_price == 4800
        # traffic: 5000 * 30% = 1500 discount -> 3500 per month x 1
        assert result.traffic_price == 3500
        # devices: 2 extra x 4000 = 8000, 0% discount -> 8000
        assert result.devices_price == 8000
        # total group discount = 1000 + 1200 + 1500 + 0 = 3700
        assert result.promo_group_discount == 3700
        assert result.final_total == 9000 + 4800 + 3500 + 8000

    @pytest.mark.asyncio
    async def test_classic_user_none(self):
        """When user=None, no discounts are applied."""
        engine = PricingEngine()
        db = AsyncMock()
        subscription = MagicMock()
        subscription.tariff_id = None
        subscription.tariff = None
        subscription.connected_squads = []
        subscription.traffic_limit_gb = 0
        subscription.purchased_traffic_gb = 0
        subscription.device_limit = 1
        with (
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=0),
            patch('app.services.pricing_engine.settings') as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 15000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.get_traffic_price.return_value = 0
            ms.PRICE_PER_DEVICE = 0
            ms.DEFAULT_DEVICE_LIMIT = 1
            ms.is_traffic_fixed.return_value = False
            result = await engine.calculate_renewal_price(db, subscription, 30, user=None)
        assert result.final_total == 15000
        assert result.promo_group_discount == 0
        assert result.promo_offer_discount == 0


class TestServerPromoGroupFiltering:
    @pytest.mark.asyncio
    async def test_server_not_allowed_for_promo_group(self):
        """Server with restricted promo groups still charges real price."""
        engine = PricingEngine()
        db = AsyncMock()
        pg_mock = MagicMock()
        pg_mock.id = 99
        server = _make_server(price_kopeks=5000, squad_uuid='uuid-1', allowed_promo_groups=[pg_mock])
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]):
            total, details = await engine._calculate_servers_price(['uuid-1'], db, promo_group_id=5)
        assert total == 5000  # real price still charged
        assert details[0]['status'] == 'not_allowed'

    @pytest.mark.asyncio
    async def test_server_empty_allowed_groups_is_open(self):
        """Server with empty allowed_promo_groups is available to all."""
        engine = PricingEngine()
        db = AsyncMock()
        server = _make_server(price_kopeks=5000, squad_uuid='uuid-1', allowed_promo_groups=[])
        with patch('app.services.pricing_engine.get_server_squads_by_uuids', return_value=[server]):
            total, details = await engine._calculate_servers_price(['uuid-1'], db, promo_group_id=5)
        assert total == 5000
        assert details[0]['status'] == 'available'


class TestFromPayloadRoundTrip:
    def test_renewal_pricing_snapshot_roundtrip(self):
        """RenewalPricing serialized via asdict() is correctly restored by from_payload()."""
        import dataclasses

        from app.services.subscription_renewal_service import SubscriptionRenewalPricing

        pricing = RenewalPricing(
            base_price=29000,
            servers_price=5000,
            traffic_price=3000,
            devices_price=0,
            promo_group_discount=2000,
            promo_offer_discount=800,
            final_total=34200,
            period_days=30,
            is_tariff_mode=False,
            breakdown={
                'server_ids': [1, 2],
                'servers_individual_prices': [5000, 3000],
                'offer_discount_pct': 5,
            },
        )
        payload = dataclasses.asdict(pricing)
        restored = SubscriptionRenewalPricing.from_payload(payload)

        assert restored.final_total == 34200
        assert restored.period_days == 30
        assert restored.promo_discount_value == 800  # mapped from promo_offer_discount
        assert restored.server_ids == [1, 2]
        assert restored.details.get('servers_individual_prices') == [5000, 3000]
        assert restored.months == 1
        assert restored.per_month == 34200


# ---------------------------------------------------------------------------
# Patch-target constants used in new tests below
# ---------------------------------------------------------------------------
SERVERS_BATCH_PATH = 'app.services.pricing_engine.get_server_squads_by_uuids'
SETTINGS_PATH = 'app.services.pricing_engine.settings'


class TestFromPayloadLegacyRoundTrip:
    def test_legacy_to_payload_roundtrip(self):
        """Legacy SubscriptionRenewalPricing.to_payload() -> from_payload() preserves all fields."""
        from app.services.subscription_renewal_service import SubscriptionRenewalPricing, build_renewal_period_id

        original = SubscriptionRenewalPricing(
            period_days=30,
            period_id=build_renewal_period_id(30),
            months=1,
            base_original_total=15000,
            discounted_total=12000,
            final_total=10800,
            promo_discount_value=1200,
            promo_discount_percent=10,
            overall_discount_percent=28,
            per_month=10800,
            server_ids=[1, 2, 3],
            details={'servers_individual_prices': [5000, 3000, 2000]},
        )
        payload = original.to_payload()
        restored = SubscriptionRenewalPricing.from_payload(payload)

        assert restored.period_days == original.period_days
        assert restored.period_id == original.period_id
        assert restored.months == original.months
        assert restored.base_original_total == original.base_original_total
        assert restored.discounted_total == original.discounted_total
        assert restored.final_total == original.final_total
        assert restored.promo_discount_value == original.promo_discount_value
        assert restored.promo_discount_percent == original.promo_discount_percent
        assert restored.overall_discount_percent == original.overall_discount_percent
        assert restored.per_month == original.per_month
        assert restored.server_ids == original.server_ids


class TestOriginalPriceIdentity:
    @pytest.mark.asyncio
    async def test_tariff_mode_identity(self):
        """final_total + promo_group_discount + promo_offer_discount == undiscounted subtotal."""
        engine = PricingEngine()
        db = AsyncMock()
        tariff = MagicMock()
        tariff.id = 1
        tariff.period_prices = {'30': 20000}
        tariff.device_price_kopeks = 3000
        tariff.device_limit = 1
        sub = MagicMock()
        sub.tariff_id = 1
        sub.tariff = tariff
        sub.device_limit = 3  # 2 extra
        user = MagicMock()
        promo_group = MagicMock()
        promo_group.get_discount_percent = MagicMock(return_value=25)
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group

        with patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=15):
            result = await engine.calculate_renewal_price(db, sub, 30, user=user)

        subtotal = 20000 + 2 * 3000  # 26000
        assert result.final_total + result.promo_group_discount + result.promo_offer_discount == subtotal

    @pytest.mark.asyncio
    async def test_classic_mode_identity(self):
        """final_total + promo_group_discount + promo_offer_discount == undiscounted total in classic mode."""
        engine = PricingEngine()
        db = AsyncMock()
        sub = MagicMock()
        sub.tariff_id = None
        sub.tariff = None
        sub.connected_squads = ['uuid-s1']
        sub.traffic_limit_gb = 50
        sub.purchased_traffic_gb = 0
        sub.device_limit = 2  # 1 extra

        user = MagicMock()
        promo_group = MagicMock()
        promo_group.get_discount_percent = MagicMock(return_value=20)
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        user.promo_group_id = 1

        server = _make_server(price_kopeks=4000, squad_uuid='uuid-s1')

        with (
            patch(SERVERS_BATCH_PATH, return_value=[server]),
            patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=10),
            patch(SETTINGS_PATH) as ms,
            patch('app.services.pricing_engine.CLASSIC_PERIOD_PRICES', {30: 10000}),
            patch('app.services.pricing_engine.PERIOD_PRICES', {}),
        ):
            ms.PRICE_PER_DEVICE = 5000
            ms.DEFAULT_DEVICE_LIMIT = 1
            ms.get_traffic_price.return_value = 3000
            ms.is_traffic_fixed.return_value = False
            ms.DEFAULT_TRAFFIC_LIMIT_GB = 50

            result = await engine.calculate_renewal_price(db, sub, 30, user=user)

        # Reconstruct original undiscounted total
        original = result.final_total + result.promo_group_discount + result.promo_offer_discount
        # original should equal base_original + servers_original + traffic_original + devices_original
        expected_original = 10000 + 4000 + 3000 + 5000  # 22000
        assert original == expected_original

    @pytest.mark.asyncio
    async def test_original_total_property_tariff(self):
        """original_total property returns correct value."""
        engine = PricingEngine()
        db = AsyncMock()
        tariff = MagicMock()
        tariff.id = 1
        tariff.period_prices = {'30': 20000}
        tariff.device_price_kopeks = None
        tariff.device_limit = 1
        sub = MagicMock()
        sub.tariff_id = 1
        sub.tariff = tariff
        sub.device_limit = 1
        user = MagicMock()
        promo_group = MagicMock()
        promo_group.get_discount_percent = MagicMock(return_value=10)
        user.promo_group = promo_group
        user.get_primary_promo_group.return_value = promo_group
        sub.tariff.is_daily = False
        sub.tariff.can_purchase_custom_days.return_value = False
        with patch('app.services.pricing_engine.get_user_active_promo_discount_percent', return_value=5):
            result = await engine.calculate_renewal_price(db, sub, 30, user=user)
        assert result.original_total == 20000  # undiscounted subtotal
