from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from app.config import CLASSIC_PERIOD_PRICES, PERIOD_PRICES, settings
from app.database.crud.server_squad import get_server_squads_by_uuids
from app.utils.pricing_utils import calculate_months_from_days
from app.utils.promo_offer import get_user_active_promo_discount_percent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.database.models import Subscription, Tariff, User


logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class TariffBreakdown:
    """Typed breakdown for tariff mode pricing."""

    tariff_id: int
    extra_devices: int
    group_discount_pct: dict[str, int]
    offer_discount_pct: int
    months_in_period: int = 1
    # Explainability: which discount source actually determined the base/period price,
    # and what each competing source offered — so a support agent or admin can look at
    # a transaction and see exactly why that price was charged, not reverse-engineer it.
    base_discount_source: str = 'group'  # 'tariff' | 'group' | 'offer'
    tariff_period_discount_pct: int = 0  # what the tariff's own built-in price offered


@dataclass(frozen=True)
class ClassicBreakdown:
    """Typed breakdown for classic mode pricing."""

    months_in_period: int
    servers: list[dict[str, Any]]
    servers_individual_prices: list[int]
    server_ids: list[int]
    base_traffic_gb: int
    purchased_traffic_gb: int
    extra_devices: int
    # Per-category discount percents (period/servers/traffic/devices)
    group_discount_pct: dict[str, int]
    offer_discount_pct: int
    # Original (pre-discount) prices — used by classic_pricing_to_purchase_details()
    base_price_original: int = 0
    traffic_price_per_month: int = 0
    servers_price_per_month: int = 0
    devices_price_per_month: int = 0


@dataclass(frozen=True)
class RenewalPricing:
    """Immutable result of a renewal price calculation."""

    base_price: int  # kopeks
    servers_price: int  # kopeks
    traffic_price: int  # kopeks
    devices_price: int  # kopeks
    # Total discount NOT from the personal one-shot offer (i.e. whichever of the
    # tariff's own built-in discount or the promo-group's discount actually applied
    # — see breakdown['base_discount_source'] in tariff mode to tell which one it was;
    # this field alone does not distinguish them).
    promo_group_discount: int  # kopeks deducted
    promo_offer_discount: int  # kopeks deducted
    final_total: int  # kopeks — amount to charge
    period_days: int
    is_tariff_mode: bool
    breakdown: dict[str, Any] = field(default_factory=dict)

    @property
    def original_total(self) -> int:
        """Price before all discounts (group + offer)."""
        return self.final_total + self.promo_group_discount + self.promo_offer_discount


@dataclass(frozen=True)
class TariffSwitchResult:
    """Immutable result of a tariff switch cost calculation."""

    upgrade_cost: int  # kopeks — amount to charge (0 if downgrade/same)
    is_upgrade: bool  # True if new tariff is more expensive
    raw_cost: int  # kopeks — cost before discounts (for UI display)
    group_discount_pct: int
    offer_discount_pct: int
    new_period_days: int = 0  # 0 = keep current end date, >0 = set new subscription period

    @property
    def discount_value(self) -> int:
        """Сумма скидки в копейках."""
        return self.raw_cost - self.upgrade_cost

    @property
    def effective_discount_pct(self) -> int:
        """Эффективный процент скидки (стекинг group + offer)."""
        if self.raw_cost <= 0:
            return 0
        return round(self.discount_value * 100 / self.raw_cost)


class PricingEngine:
    """Unified pricing engine for all subscription renewal calculations."""

    @staticmethod
    def apply_discount(amount_kopeks: int, percent: int) -> int:
        """Apply percentage discount with integer arithmetic.
        Clamps percent to [0, 100]. Uses floor division."""
        percent = max(0, min(100, percent))
        discount = amount_kopeks * percent // 100
        return amount_kopeks - discount

    @staticmethod
    def _combine_group_and_offer(
        raw_amount: int,
        group_discounted_amount: int,
        offer_percent: int,
        mode: str | None = None,
        tariff_amount: int | None = None,
    ) -> tuple[int, int, int, bool, str]:
        """Combine a promo-group discount with a personal promo-offer discount, and
        optionally a tariff's own built-in price for this component.

        Strategy is controlled by DISCOUNT_STACKING_MODE (settings.get_discount_stacking_mode()
        when `mode` is not passed explicitly):
        - 'multiply' (default, legacy): stack sequentially — offer applies on top
          of the already group-discounted amount. `tariff_amount` is IGNORED in
          this mode — 'multiply' assumes the caller already baked the tariff's own
          price into `group_discounted_amount`/`raw_amount`, exactly as before this
          parameter existed.
        - 'max': take whichever of {tariff's own price (if given), group-discounted
          amount, offer-discounted amount} is CHEAPEST; apply only that one, never
          combine them. Ties favor the more specific/personal source: group beats
          tariff, offer beats group.

        Returns (final_amount, group_discount_value, offer_discount_value, offer_won, source).
        `offer_won` is True only when the offer was the winning source (callers with
        per-component prices must reset them to their raw values in that case).
        `source` is one of 'tariff', 'group', 'offer' — which single discount actually
        determined the price. In 'multiply' mode it is always 'group' (both discounts
        are always applied together there, so there is no true single winner, but
        'group' is the closest legacy-compatible label — that's the axis 'multiply'
        always builds onto).
        """
        if mode is None:
            mode = settings.get_discount_stacking_mode()

        if mode == 'max':
            if tariff_amount is not None and tariff_amount < group_discounted_amount:
                best_amount, best_source = tariff_amount, 'tariff'
            else:
                best_amount, best_source = group_discounted_amount, 'group'
            group_discount_value = raw_amount - best_amount

            after_offer_only = PricingEngine.apply_discount(raw_amount, offer_percent)
            offer_discount_value = raw_amount - after_offer_only
            if offer_discount_value >= group_discount_value:
                return after_offer_only, 0, offer_discount_value, True, 'offer'
            return best_amount, group_discount_value, 0, False, best_source

        # 'multiply' (default / legacy) — tariff_amount ignored, unchanged behavior
        group_discount_value = raw_amount - group_discounted_amount
        after_offer = PricingEngine.apply_discount(group_discounted_amount, offer_percent)
        offer_discount_value = group_discounted_amount - after_offer
        return after_offer, group_discount_value, offer_discount_value, False, 'group'

    @staticmethod
    def apply_stacked_discounts(
        amount: int,
        group_percent: int,
        offer_percent: int,
        mode: str | None = None,
    ) -> tuple[int, int, int]:
        """Apply promo-group discount, then combine with promo-offer per DISCOUNT_STACKING_MODE.
        Returns (final_amount, group_discount_value, offer_discount_value)."""
        after_group = PricingEngine.apply_discount(amount, group_percent)
        final, group_val, offer_val, _offer_won, _source = PricingEngine._combine_group_and_offer(
            amount, after_group, offer_percent, mode
        )
        return final, group_val, offer_val

    @staticmethod
    def resolve_promo_group(user: User | None):
        """Resolve primary promo group: get_primary_promo_group() first, fallback to user.promo_group."""
        if not user:
            return None
        if hasattr(user, 'get_primary_promo_group'):
            pg = user.get_primary_promo_group()
            if pg is not None:
                return pg
        return getattr(user, 'promo_group', None)

    @staticmethod
    def get_addon_discount_percent(
        user: User | None,
        category: str,
        period_days_hint: int | None = None,
        *,
        promo_group: PromoGroup | None = None,
    ) -> int:
        """Return addon discount percent for a given category.

        Uses promo_group.get_discount_percent() which handles is_default fallback.
        Checks apply_discounts_to_addons flag. Returns 0 if no discount.

        If promo_group is provided explicitly, it takes precedence over
        resolving from user (useful when caller already resolved the group).
        """
        if promo_group is None:
            if not user:
                return 0
            promo_group = PricingEngine.resolve_promo_group(user)

        if not promo_group:
            return 0

        if not getattr(promo_group, 'apply_discounts_to_addons', True):
            return 0

        if hasattr(promo_group, 'get_discount_percent'):
            return promo_group.get_discount_percent(category, period_days_hint)

        # Fallback for promo groups without get_discount_percent
        mapping = {
            'traffic': 'traffic_discount_percent',
            'servers': 'server_discount_percent',
            'devices': 'device_discount_percent',
        }
        attr = mapping.get(category)
        if attr:
            return max(0, min(100, int(getattr(promo_group, attr, 0) or 0)))
        return 0

    @staticmethod
    def calculate_traffic_discount(
        base_price: int,
        user: User | None,
        period_days_hint: int | None = None,
    ) -> tuple[int, int, int]:
        """Apply traffic addon discount from user's promo group.

        Checks apply_discounts_to_addons flag. Uses integer arithmetic.
        Uses get_discount_percent() for correct is_default fallback.
        Returns: (final_price, discount_value, discount_percent).
        """
        if not user or base_price <= 0:
            return base_price, 0, 0

        pct = PricingEngine.get_addon_discount_percent(user, 'traffic', period_days_hint)
        if pct <= 0:
            return base_price, 0, 0

        final = PricingEngine.apply_discount(base_price, pct)
        return final, base_price - final, pct

    # ------------------------------------------------------------------
    # Tariff switch
    # ------------------------------------------------------------------

    @staticmethod
    def get_tariff_daily_rate_fraction(tariff: Tariff, target_days: int) -> tuple[int, int]:
        """Дневная ставка тарифа как (price, period_days) для целочисленных вычислений.

        Возвращает числитель и знаменатель дроби price/period_days,
        чтобы избежать float-ошибок в финансовых расчётах.
        """
        periods = tariff.get_available_periods()
        if not periods:
            return 0, 1
        best_period = min(periods, key=lambda p: abs(p - target_days))
        price = tariff.get_price_for_period(best_period)
        if not price or best_period <= 0:
            return 0, 1
        return price, best_period

    def calculate_tariff_switch_cost(
        self,
        current_tariff: Tariff,
        new_tariff: Tariff,
        remaining_days: int,
        *,
        user: User | None = None,
    ) -> TariffSwitchResult:
        """Рассчитывает стоимость переключения тарифа.

        Автоматически определяет тип переключения:
        - periodic→daily: оплата первого дня (daily_price_kopeks)
        - daily→periodic: оплата кратчайшего периода нового тарифа
        - periodic→periodic: пропорциональная разница дневных ставок × remaining_days

        Для всех типов переключений скидки (group + offer) применяются stacked.
        """
        current_is_daily = getattr(current_tariff, 'is_daily', False) if current_tariff else False
        new_is_daily = getattr(new_tariff, 'is_daily', False)

        # Daily tariff edge cases
        if not current_is_daily and new_is_daily:
            return self._calculate_switch_to_daily(new_tariff, remaining_days, user=user)
        if current_is_daily and not new_is_daily:
            return self._calculate_switch_from_daily(new_tariff, remaining_days, user=user)
        if current_is_daily and new_is_daily:
            # Daily → Daily: бесплатное переключение, cron начислит новую цену завтра
            return TariffSwitchResult(
                upgrade_cost=0,
                is_upgrade=False,
                raw_cost=0,
                group_discount_pct=0,
                offer_discount_pct=0,
                new_period_days=1,
            )

        # --- Periodic → Periodic ---

        # Early return: нечего считать при нулевом остатке
        if remaining_days <= 0:
            return TariffSwitchResult(
                upgrade_cost=0,
                is_upgrade=False,
                raw_cost=0,
                group_discount_pct=0,
                offer_discount_pct=0,
                new_period_days=0,
            )

        # Целочисленная арифметика (без float round-trip):
        # raw_cost = (new_p/new_d - cur_p/cur_d) * remaining
        #          = (new_p * cur_d - cur_p * new_d) * remaining / (new_d * cur_d)
        # Floor division (//) округляет дробные копейки вниз — в пользу пользователя.
        cur_price, cur_period = self.get_tariff_daily_rate_fraction(current_tariff, remaining_days)
        new_price, new_period = self.get_tariff_daily_rate_fraction(new_tariff, remaining_days)

        numerator = (new_price * cur_period - cur_price * new_period) * remaining_days
        denominator = new_period * cur_period
        raw_cost = max(0, numerator // denominator)

        if numerator <= 0:
            return TariffSwitchResult(
                upgrade_cost=0,
                is_upgrade=False,
                raw_cost=0,
                group_discount_pct=0,
                offer_discount_pct=0,
                new_period_days=0,
            )

        # Resolve discounts via resolve_promo_group (get_primary_promo_group first)
        group_pct = 0
        offer_pct = 0
        if user:
            promo_group = self.resolve_promo_group(user)
            if promo_group is not None:
                best_period = min(
                    current_tariff.get_available_periods() or [30],
                    key=lambda p: abs(p - remaining_days),
                )
                group_pct = promo_group.get_discount_percent('period', best_period)
            offer_pct = get_user_active_promo_discount_percent(user)

        # Применяем stacked скидки к итоговой сумме напрямую (без float round-trip)
        if group_pct > 0 or offer_pct > 0:
            upgrade_cost, _, _ = self.apply_stacked_discounts(raw_cost, group_pct, offer_pct)
        else:
            upgrade_cost = raw_cost

        return TariffSwitchResult(
            upgrade_cost=upgrade_cost,
            is_upgrade=True,
            raw_cost=raw_cost,
            group_discount_pct=group_pct,
            offer_discount_pct=offer_pct,
            new_period_days=0,
        )

    def _calculate_switch_to_daily(
        self,
        new_tariff: Tariff,
        remaining_days: int,
        *,
        user: User | None = None,
    ) -> TariffSwitchResult:
        """Periodic → Daily: оплата первого дня с group + offer discount."""
        daily_price = getattr(new_tariff, 'daily_price_kopeks', 0) or 0
        if daily_price <= 0:
            return TariffSwitchResult(
                upgrade_cost=0,
                is_upgrade=False,
                raw_cost=0,
                group_discount_pct=0,
                offer_discount_pct=0,
                new_period_days=1,
            )

        group_pct = 0
        offer_pct = 0
        if user:
            promo_group = self.resolve_promo_group(user)
            if promo_group:
                period_hint = remaining_days if remaining_days > 0 else 30
                group_pct = promo_group.get_discount_percent('period', period_hint)
            offer_pct = get_user_active_promo_discount_percent(user)

        if group_pct > 0 or offer_pct > 0:
            upgrade_cost, _, _ = self.apply_stacked_discounts(daily_price, group_pct, offer_pct)
        else:
            upgrade_cost = daily_price

        return TariffSwitchResult(
            upgrade_cost=upgrade_cost,
            is_upgrade=upgrade_cost > 0,
            raw_cost=daily_price,
            group_discount_pct=group_pct,
            offer_discount_pct=offer_pct,
            new_period_days=1,
        )

    def _calculate_switch_from_daily(
        self,
        new_tariff: Tariff,
        remaining_days: int,
        *,
        user: User | None = None,
    ) -> TariffSwitchResult:
        """Daily → Periodic: оплата кратчайшего периода нового тарифа с group + offer discount."""
        min_period_days = 30
        min_period_price = 0
        if new_tariff.period_prices:
            min_period_days = min(int(k) for k in new_tariff.period_prices.keys())
            min_period_price = new_tariff.period_prices.get(str(min_period_days), 0) or 0

        if min_period_price <= 0:
            return TariffSwitchResult(
                upgrade_cost=0,
                is_upgrade=False,
                raw_cost=0,
                group_discount_pct=0,
                offer_discount_pct=0,
                new_period_days=min_period_days,
            )

        group_pct = 0
        offer_pct = 0
        if user:
            promo_group = self.resolve_promo_group(user)
            if promo_group:
                group_pct = promo_group.get_discount_percent('period', min_period_days)
            offer_pct = get_user_active_promo_discount_percent(user)

        if group_pct > 0 or offer_pct > 0:
            upgrade_cost, _, _ = self.apply_stacked_discounts(min_period_price, group_pct, offer_pct)
        else:
            upgrade_cost = min_period_price

        return TariffSwitchResult(
            upgrade_cost=upgrade_cost,
            is_upgrade=upgrade_cost > 0,
            raw_cost=min_period_price,
            group_discount_pct=group_pct,
            offer_discount_pct=offer_pct,
            new_period_days=min_period_days,
        )

    async def _calculate_servers_price(
        self,
        country_uuids: list[str],
        db: AsyncSession,
        *,
        promo_group_id: int | None = None,
    ) -> tuple[int, list[dict]]:
        """Calculate total server price from connected squad UUIDs.

        Uses a single batch query instead of N+1 individual queries.
        ALWAYS uses real price_kopeks even when server is unavailable
        or full. Only orphaned UUIDs (not found in DB) get price=0.
        """
        if not country_uuids:
            return 0, []

        try:
            servers = await get_server_squads_by_uuids(db, country_uuids)
        except Exception as e:  # intentional broad catch: pricing must not crash on DB errors, servers_price=0 is safe (user pays less)
            logger.error('Ошибка пакетной загрузки серверов', error=str(e), squad_uuids=country_uuids)
            return 0, [{'uuid': uuid, 'id': None, 'price': 0, 'status': 'error'} for uuid in country_uuids]

        server_map = {s.squad_uuid: s for s in servers}

        total_price = 0
        details: list[dict] = []

        for uuid in country_uuids:
            server = server_map.get(uuid)
            if server is None:
                logger.error('Сервер не найден в БД', squad_uuid=uuid)
                details.append({'uuid': uuid, 'id': None, 'price': 0, 'status': 'not_found'})
                continue

            price = server.price_kopeks or 0
            status = 'available'

            if not server.is_available:
                status = 'unavailable'
                logger.warning(
                    'Сервер недоступен, используем реальную цену',
                    squad_uuid=uuid,
                    price_kopeks=price,
                )
            elif server.is_full:
                status = 'full'
                logger.warning(
                    'Сервер переполнен, используем реальную цену',
                    squad_uuid=uuid,
                    price_kopeks=price,
                )
            elif promo_group_id is not None:
                allowed_ids = [pg.id for pg in (server.allowed_promo_groups or [])]
                if allowed_ids and promo_group_id not in allowed_ids:
                    status = 'not_allowed'
                    logger.warning(
                        'Сервер недоступен для промогруппы, используем реальную цену',
                        squad_uuid=uuid,
                        promo_group_id=promo_group_id,
                        price_kopeks=price,
                    )

            total_price += price
            details.append({'uuid': uuid, 'id': server.id, 'price': price, 'status': status})

        return total_price, details

    def _calculate_traffic_price(
        self,
        traffic_limit_gb: int,
        purchased_traffic_gb: int,
    ) -> int:
        """Calculate traffic price, separating base from purchased GB.
        Prevents purchased top-ups from inflating the tier lookup."""
        total_gb = traffic_limit_gb or 0
        purchased_gb = purchased_traffic_gb or 0

        # 0 = unlimited traffic — has its own price tier, return directly
        if total_gb == 0:
            return settings.get_traffic_price(0)

        base_gb = max(0, total_gb - purchased_gb)

        base_price = settings.get_traffic_price(base_gb) if base_gb > 0 else 0
        purchased_price = settings.get_traffic_price(purchased_gb) if purchased_gb > 0 else 0

        return base_price + purchased_price

    # ------------------------------------------------------------------
    # Main public method
    # ------------------------------------------------------------------

    async def calculate_renewal_price(
        self,
        db: AsyncSession,
        subscription: Subscription,
        period_days: int,
        *,
        user: User | None = None,
    ) -> RenewalPricing:
        """Calculate renewal price for a subscription.

        Routes to tariff mode (subscription has a tariff) or classic mode
        (legacy env-based pricing).  Stacked discounts (promo-group then
        promo-offer) are applied in both modes.
        """
        if not isinstance(period_days, int) or period_days <= 0:
            raise ValueError(f'Invalid period_days: {period_days}')

        if subscription.tariff_id is not None:
            if subscription.tariff is None:
                logger.error(
                    'tariff_id set but tariff relationship not loaded, falling back to classic mode',
                    subscription_id=getattr(subscription, 'id', None),
                    tariff_id=subscription.tariff_id,
                )
            else:
                return await self._calculate_tariff_mode(db, subscription, period_days, user=user)
        return await self._calculate_classic_mode(db, subscription, period_days, user=user)

    # ------------------------------------------------------------------
    # Tariff mode
    # ------------------------------------------------------------------

    async def _calculate_tariff_mode(
        self,
        db: AsyncSession,
        subscription: Subscription,
        period_days: int,
        *,
        user: User | None = None,
    ) -> RenewalPricing:
        """Price calculation when subscription is linked to a Tariff."""
        tariff = subscription.tariff
        device_limit = subscription.device_limit or 0
        return await self._calculate_tariff_core(
            tariff,
            period_days,
            device_limit,
            user=user,
        )

    async def _calculate_tariff_core(
        self,
        tariff: Tariff,
        period_days: int,
        device_limit: int,
        *,
        custom_traffic_gb: int | None = None,
        user: User | None = None,
    ) -> RenewalPricing:
        """Core tariff pricing logic (raw params, no Subscription needed).

        Per-category discounts:
        - 'period' → base tariff price
        - 'devices' → extra device cost
        Promo-offer discount applied on the discounted subtotal.
        Device cost is monthly × months_in_period.
        """
        months = calculate_months_from_days(period_days)

        # --- Base price ---
        is_daily = getattr(tariff, 'is_daily', False)
        if is_daily and period_days <= 1:
            base_price = int(getattr(tariff, 'daily_price_kopeks', 0) or 0)
        else:
            period_prices: dict = tariff.period_prices or {}
            base_price = int(period_prices.get(str(period_days), 0) or 0)
            if base_price == 0 and hasattr(tariff, 'get_price_for_custom_days'):
                if hasattr(tariff, 'can_purchase_custom_days') and tariff.can_purchase_custom_days():
                    custom_price = tariff.get_price_for_custom_days(period_days)
                    if custom_price is not None:
                        base_price = int(custom_price)

        # --- Extra devices (monthly × months) ---
        device_price_per_unit = (
            tariff.device_price_kopeks if tariff.device_price_kopeks is not None else settings.PRICE_PER_DEVICE
        )
        tariff_device_limit = tariff.device_limit or 0
        extra_devices = max(0, (device_limit or 0) - tariff_device_limit)
        if is_daily and period_days <= 1:
            devices_price = extra_devices * device_price_per_unit
        else:
            devices_price = extra_devices * device_price_per_unit * months

        # --- Custom traffic (tariff add-on, uses addon discount path) ---
        traffic_price = 0
        if custom_traffic_gb is not None and hasattr(tariff, 'get_price_for_custom_traffic'):
            raw_traffic = tariff.get_price_for_custom_traffic(custom_traffic_gb)
            if raw_traffic and raw_traffic > 0:
                traffic_price = int(raw_traffic)

        # --- Per-category group discounts ---
        period_pct = 0
        devices_pct = 0
        promo_group = self.resolve_promo_group(user)
        if promo_group is not None:
            period_pct = promo_group.get_discount_percent('period', period_days)
            devices_pct = promo_group.get_discount_percent('devices', period_days)

        offer_pct = get_user_active_promo_discount_percent(user) if user else 0
        mode = settings.get_discount_stacking_mode()

        # Nominal base price (monthly x months) — the TRUE reference price before ANY
        # discount, including the tariff's own. Only meaningful/used in 'max' mode; in
        # 'multiply' mode the tariff's own period_prices[days] value is used directly,
        # exactly as before this feature existed — DO NOT let nominal_base leak into
        # 'multiply' mode's math, that would silently change already-shipped legacy
        # behavior for every customer, not just promo-group ones.
        monthly_price = 0
        if not (is_daily and period_days <= 1):
            monthly_price = int((tariff.period_prices or {}).get('30', 0) or 0)
        nominal_base = monthly_price * months if monthly_price > 0 else base_price

        tariff_period_discount_pct = 0
        if mode == 'max':
            # Group's period discount competes against the TRUE nominal price, not
            # against the tariff's own already-discounted price — that's the whole
            # point of 'max' mode: the tariff's own discount becomes a candidate,
            # not something the group discount silently compounds with.
            discounted_base_via_group = self.apply_discount(nominal_base, period_pct)
            raw_base = nominal_base
            tariff_amount_component: int | None = base_price
            if nominal_base > 0:
                tariff_period_discount_pct = round((nominal_base - base_price) * 100 / nominal_base)
        else:
            # 'multiply' (legacy, unchanged): group discount compounds with the
            # tariff's own already-discounted price, exactly as before.
            discounted_base_via_group = self.apply_discount(base_price, period_pct)
            raw_base = base_price
            tariff_amount_component = None

        discounted_devices = self.apply_discount(devices_price, devices_pct)

        # Traffic uses addon discount (checks apply_discounts_to_addons flag)
        discounted_traffic = traffic_price
        if traffic_price > 0 and user:
            discounted_traffic, _, _ = self.calculate_traffic_discount(traffic_price, user)

        # Three competing discount sources for the base/period component ('max' mode
        # only — see _combine_group_and_offer docstring for 'multiply' mode's
        # unchanged, compounding behavior):
        # (1) the tariff's own built-in "pay for N months, save X%" price (base_price),
        # (2) the promo-group's period discount, computed against the TRUE nominal
        #     price (discounted_base_via_group),
        # (3) the personal promo-offer discount.
        raw_subtotal = raw_base + devices_price + traffic_price
        group_subtotal = discounted_base_via_group + discounted_devices + discounted_traffic
        tariff_subtotal = (
            tariff_amount_component + discounted_devices + discounted_traffic
            if tariff_amount_component is not None
            else None
        )
        final_total, total_group_discount, offer_discount, offer_won, base_source = self._combine_group_and_offer(
            raw_subtotal, group_subtotal, offer_pct, mode=mode, tariff_amount=tariff_subtotal
        )
        if offer_won:
            # Offer applied instead of any other discount — reset components AND their
            # percentages to raw/zero (no other discount was applied to any of them).
            discounted_base, discounted_devices, discounted_traffic = raw_base, devices_price, traffic_price
            period_pct = devices_pct = 0
        elif base_source == 'tariff':
            # The tariff's own built-in discount was cheaper than the group's — use it,
            # and report 0% group discount (the group's period_pct wasn't what applied,
            # even if a group discount was nominally configured for this period).
            discounted_base = base_price
            period_pct = 0
        else:
            # base_source == 'group' — the group's period discount won (or 'multiply'
            # mode, where this branch always runs and period_pct/discounted_base_via_group
            # already reflect the correct, unchanged legacy compounding formula).
            discounted_base = discounted_base_via_group

        breakdown = dataclasses.asdict(
            TariffBreakdown(
                tariff_id=tariff.id,
                extra_devices=extra_devices,
                group_discount_pct={'period': period_pct, 'devices': devices_pct},
                offer_discount_pct=offer_pct,
                months_in_period=months,
                base_discount_source=base_source,
                tariff_period_discount_pct=tariff_period_discount_pct,
            )
        )

        if mode == 'max':
            logger.info(
                'Tariff period price resolved (max mode)',
                tariff_id=tariff.id,
                period_days=period_days,
                nominal_base=nominal_base / 100,
                tariff_price=base_price / 100,
                tariff_pct=tariff_period_discount_pct,
                group_pct=period_pct if base_source == 'group' else 0,
                offer_pct=offer_pct,
                winning_source=base_source,
                discount_value_source=base_source,
                final_base_price=discounted_base / 100,
            )

        if final_total < 0:
            logger.warning(
                'Negative final_total in tariff mode, clamping to 0',
                final_total=final_total,
                raw_subtotal=raw_subtotal,
                period_pct=period_pct,
                devices_pct=devices_pct,
                offer_pct=offer_pct,
            )

        return RenewalPricing(
            base_price=discounted_base,
            servers_price=0,
            traffic_price=discounted_traffic,
            devices_price=discounted_devices,
            promo_group_discount=total_group_discount,
            promo_offer_discount=offer_discount,
            final_total=max(0, final_total),
            period_days=period_days,
            is_tariff_mode=True,
            breakdown=breakdown,
        )

    async def calculate_tariff_purchase_price(
        self,
        tariff: Tariff,
        period_days: int,
        *,
        device_limit: int | None = None,
        custom_traffic_gb: int | None = None,
        user: User | None = None,
    ) -> RenewalPricing:
        """Calculate price for a tariff purchase (new or renewal).

        Public method that delegates to _calculate_tariff_core.
        If device_limit is None, uses the tariff's included limit (no extra devices).
        """
        effective_device_limit = device_limit if device_limit is not None else (tariff.device_limit or 0)
        return await self._calculate_tariff_core(
            tariff,
            period_days,
            effective_device_limit,
            custom_traffic_gb=custom_traffic_gb,
            user=user,
        )

    # ------------------------------------------------------------------
    # Classic mode
    # ------------------------------------------------------------------

    async def _calculate_classic_core(
        self,
        db: AsyncSession,
        period_days: int,
        connected_squads: list[str],
        traffic_limit_gb: int,
        device_limit: int,
        *,
        purchased_traffic_gb: int = 0,
        user: User | None = None,
    ) -> RenewalPricing:
        """Core classic-mode pricing logic (raw params, no Subscription needed).

        Uses CLASSIC_PERIOD_PRICES from settings, falling back to the
        global PERIOD_PRICES dict during migration.

        Per-category discounts (period, servers, traffic, devices) are
        applied separately to each component. Servers, traffic, and
        devices are monthly prices multiplied by months_in_period.
        Promo-offer discount is applied on the subtotal.
        """
        months = calculate_months_from_days(period_days)

        # --- Base period price (already includes full period) ---
        base_price_original = CLASSIC_PERIOD_PRICES.get(period_days)
        if base_price_original is None:
            base_price_original = PERIOD_PRICES.get(period_days, 0)
            if base_price_original > 0:
                logger.warning(
                    'CLASSIC_PERIOD_PRICES miss, falling back to PERIOD_PRICES — verify price is not from tariff regime',
                    period_days=period_days,
                    fallback_price_kopeks=base_price_original,
                )

        # --- Per-category discount percents (resolve_promo_group: get_primary_promo_group first) ---
        period_pct = 0
        servers_pct = 0
        traffic_pct = 0
        devices_pct = 0
        promo_group = self.resolve_promo_group(user)
        if promo_group is not None:
            period_pct = promo_group.get_discount_percent('period', period_days)
            servers_pct = promo_group.get_discount_percent('servers', period_days)
            traffic_pct = promo_group.get_discount_percent('traffic', period_days)
            devices_pct = promo_group.get_discount_percent('devices', period_days)

        offer_pct = get_user_active_promo_discount_percent(user) if user else 0

        # --- Base price with period discount ---
        base_price = self.apply_discount(base_price_original, period_pct)

        # --- Servers (monthly × months, with servers discount) ---
        promo_group_id = getattr(user, 'promo_group_id', None) if user else None
        servers_price_per_month, server_details = await self._calculate_servers_price(
            connected_squads,
            db,
            promo_group_id=promo_group_id,
        )
        discounted_servers_per_month = self.apply_discount(servers_price_per_month, servers_pct)
        servers_price = discounted_servers_per_month * months

        # --- Traffic (monthly × months, with traffic discount) ---
        if settings.is_traffic_fixed():
            traffic_limit_gb = settings.get_fixed_traffic_limit()
            purchased_traffic_gb = 0
        traffic_price_per_month = self._calculate_traffic_price(traffic_limit_gb, purchased_traffic_gb)
        discounted_traffic_per_month = self.apply_discount(traffic_price_per_month, traffic_pct)
        traffic_price = discounted_traffic_per_month * months

        # --- Devices (monthly × months, with devices discount) ---
        default_device_limit = settings.DEFAULT_DEVICE_LIMIT
        device_price_per_unit = settings.PRICE_PER_DEVICE
        extra_devices = max(0, (device_limit or 0) - default_device_limit)
        devices_price_per_month = extra_devices * device_price_per_unit
        discounted_devices_per_month = self.apply_discount(devices_price_per_month, devices_pct)
        devices_price = discounted_devices_per_month * months

        # --- Subtotal (category discounts already applied) ---
        subtotal = base_price + servers_price + traffic_price + devices_price

        # Group discount vs personal promo-offer: combine per DISCOUNT_STACKING_MODE
        # (default 'multiply' — offer on top of the group-discounted subtotal, unchanged
        # legacy behavior; 'max' — take whichever discount saves more, never both).
        raw_subtotal = (
            base_price_original
            + servers_price_per_month * months
            + traffic_price_per_month * months
            + devices_price_per_month * months
        )
        final_total, total_group_discount, promo_offer_discount, offer_won, _source = self._combine_group_and_offer(
            raw_subtotal, subtotal, offer_pct
        )
        if offer_won:
            # Offer applied instead of the group discount — reset components AND their
            # percentages to raw/zero (the group discount was never applied to any of
            # them). Resetting only the prices and leaving period_pct/servers_pct/
            # traffic_pct/devices_pct non-zero would make classic_pricing_to_purchase_details()
            # re-derive a phantom non-zero discount from the stale percentages below,
            # which fails validate_pricing_calculation() and breaks real purchases.
            base_price = base_price_original
            servers_price = servers_price_per_month * months
            traffic_price = traffic_price_per_month * months
            devices_price = devices_price_per_month * months
            period_pct = servers_pct = traffic_pct = devices_pct = 0

        valid_servers = [d for d in server_details if d.get('id') is not None]
        breakdown = dataclasses.asdict(
            ClassicBreakdown(
                months_in_period=months,
                servers=server_details,
                servers_individual_prices=[
                    self.apply_discount(d['price'], servers_pct) * months for d in valid_servers
                ],
                server_ids=[d['id'] for d in valid_servers],
                base_traffic_gb=max(0, traffic_limit_gb - purchased_traffic_gb),
                purchased_traffic_gb=purchased_traffic_gb,
                extra_devices=extra_devices,
                group_discount_pct={
                    'period': period_pct,
                    'servers': servers_pct,
                    'traffic': traffic_pct,
                    'devices': devices_pct,
                },
                offer_discount_pct=offer_pct,
                base_price_original=base_price_original,
                traffic_price_per_month=traffic_price_per_month,
                servers_price_per_month=servers_price_per_month,
                devices_price_per_month=devices_price_per_month,
            )
        )

        if final_total < 0:
            logger.warning(
                'Negative final_total in classic mode, clamping to 0',
                final_total=final_total,
                subtotal=subtotal,
                offer_pct=offer_pct,
            )

        return RenewalPricing(
            base_price=base_price,
            servers_price=servers_price,
            traffic_price=traffic_price,
            devices_price=devices_price,
            promo_group_discount=total_group_discount,
            promo_offer_discount=promo_offer_discount,
            final_total=max(0, final_total),
            period_days=period_days,
            is_tariff_mode=False,
            breakdown=breakdown,
        )

    async def _calculate_classic_mode(
        self,
        db: AsyncSession,
        subscription: Subscription,
        period_days: int,
        *,
        user: User | None = None,
    ) -> RenewalPricing:
        """Price calculation for legacy (non-tariff) subscriptions.

        Thin wrapper that extracts raw params from a Subscription
        and delegates to _calculate_classic_core.
        """
        connected_squads: list[str] = subscription.connected_squads or []
        traffic_limit_gb = (
            subscription.traffic_limit_gb
            if subscription.traffic_limit_gb is not None
            else settings.DEFAULT_TRAFFIC_LIMIT_GB
        )
        purchased_traffic_gb = subscription.purchased_traffic_gb or 0
        device_limit = subscription.device_limit or 0

        return await self._calculate_classic_core(
            db,
            period_days,
            connected_squads,
            traffic_limit_gb,
            device_limit,
            purchased_traffic_gb=purchased_traffic_gb,
            user=user,
        )

    async def calculate_classic_new_subscription_price(
        self,
        db: AsyncSession,
        period_days: int,
        connected_squads: list[str],
        traffic_limit_gb: int,
        device_limit: int,
        *,
        user: User | None = None,
    ) -> RenewalPricing:
        """Calculate price for a NEW classic (non-tariff) subscription.

        Like calculate_renewal_price but without requiring an existing
        Subscription object. purchased_traffic_gb is always 0.
        """
        return await self._calculate_classic_core(
            db,
            period_days,
            connected_squads,
            traffic_limit_gb,
            device_limit,
            purchased_traffic_gb=0,
            user=user,
        )

    @staticmethod
    def classic_pricing_to_purchase_details(pricing: RenewalPricing) -> dict[str, Any]:
        """Convert RenewalPricing to the legacy details dict format.

        The returned dict is compatible with build_preview_payload
        in SubscriptionPurchaseService.
        """
        bd = pricing.breakdown
        months = bd.get('months_in_period', 1) or 1
        group_pct = bd.get('group_discount_pct', {})

        base_price_original = bd.get('base_price_original', 0)
        traffic_price_per_month = bd.get('traffic_price_per_month', 0)
        servers_price_per_month = bd.get('servers_price_per_month', 0)
        devices_price_per_month = bd.get('devices_price_per_month', 0)

        period_pct = group_pct.get('period', 0)
        traffic_pct = group_pct.get('traffic', 0)
        servers_pct = group_pct.get('servers', 0)
        devices_pct = group_pct.get('devices', 0)

        base_discount_total = base_price_original - pricing.base_price
        traffic_discount_total = (
            traffic_price_per_month - PricingEngine.apply_discount(traffic_price_per_month, traffic_pct)
        ) * months
        servers_discount_total = (
            servers_price_per_month - PricingEngine.apply_discount(servers_price_per_month, servers_pct)
        ) * months
        devices_discount_total = (
            devices_price_per_month - PricingEngine.apply_discount(devices_price_per_month, devices_pct)
        ) * months

        return {
            'base_price': pricing.base_price,
            'base_price_original': base_price_original,
            'base_discount_percent': period_pct,
            'base_discount_total': base_discount_total,
            'traffic_price_per_month': traffic_price_per_month,
            'traffic_discount_percent': traffic_pct,
            'traffic_discount_total': traffic_discount_total,
            'total_traffic_price': pricing.traffic_price,
            'servers_price_per_month': servers_price_per_month,
            'servers_discount_percent': servers_pct,
            'servers_discount_total': servers_discount_total,
            'total_servers_price': pricing.servers_price,
            'devices_price_per_month': devices_price_per_month,
            'devices_discount_percent': devices_pct,
            'devices_discount_total': devices_discount_total,
            'total_devices_price': pricing.devices_price,
            'months_in_period': months,
            'servers_individual_prices': bd.get('servers_individual_prices', []),
        }


# Module-level singleton — use this instead of PricingEngine()
pricing_engine = PricingEngine()
