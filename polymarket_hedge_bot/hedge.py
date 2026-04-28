from dataclasses import dataclass

from polymarket_hedge_bot.config import RiskConfig


@dataclass(frozen=True)
class HedgeResult:
    side: str
    size_btc: float
    notional: float
    leverage: float
    isolated_margin: float
    take_profit: float
    stop_loss: float
    hedge_distance: float
    stop_distance: float
    expected_tp_profit: float
    expected_sl_loss: float
    coverage: float


def calculate_futures_hedge(
    pm_invested: float,
    btc_entry: float,
    strike: float,
    direction: str,
    config: RiskConfig,
    coverage: float | None = None,
    stop_loss: float | None = None,
    leverage: float | None = None,
    max_futures_margin: float | None = None,
) -> HedgeResult:
    coverage = config.default_coverage if coverage is None else coverage
    if coverage <= 0 or coverage > config.max_coverage:
        raise ValueError(f"coverage must be > 0 and <= {config.max_coverage}")

    if direction == "up":
        side = "LONG"
        take_profit = strike - config.tp_buffer
        hedge_distance = take_profit - btc_entry
        stop_loss = stop_loss if stop_loss is not None else btc_entry - config.min_sl_distance
        stop_distance = btc_entry - stop_loss
    elif direction == "down":
        side = "SHORT"
        take_profit = strike + config.tp_buffer
        hedge_distance = btc_entry - take_profit
        stop_loss = stop_loss if stop_loss is not None else btc_entry + config.min_sl_distance
        stop_distance = stop_loss - btc_entry
    else:
        raise ValueError("direction must be 'up' or 'down'")

    if hedge_distance <= 0:
        raise ValueError("strike is too close or already crossed for the requested hedge")
    if stop_distance <= 0:
        raise ValueError("stop_loss is invalid for hedge direction")

    size_btc = (pm_invested * coverage) / hedge_distance
    notional = size_btc * btc_entry
    if leverage is None:
        leverage = recommend_leverage(notional, max_futures_margin, config.max_leverage)
    elif leverage <= 0:
        raise ValueError("leverage must be positive")
    elif leverage > config.max_leverage:
        raise ValueError(f"leverage must be <= {config.max_leverage}")
    isolated_margin = notional / leverage

    return HedgeResult(
        side=side,
        size_btc=size_btc,
        notional=notional,
        leverage=leverage,
        isolated_margin=isolated_margin,
        take_profit=take_profit,
        stop_loss=stop_loss,
        hedge_distance=hedge_distance,
        stop_distance=stop_distance,
        expected_tp_profit=size_btc * hedge_distance,
        expected_sl_loss=size_btc * stop_distance,
        coverage=coverage,
    )


def recommend_leverage(notional: float, max_futures_margin: float | None, max_leverage: float) -> float:
    if notional <= 0:
        raise ValueError("notional must be positive")

    preferred_leverage = 5.0
    if max_futures_margin is None or max_futures_margin <= 0:
        return min(preferred_leverage, max_leverage)

    min_required = notional / max_futures_margin
    leverage = max(preferred_leverage, min_required)
    return min(max_leverage, max(1.0, leverage))


def target_remaining_pm_exposure(max_loss: float, realized_futures_loss: float) -> float:
    return max(0.0, max_loss - realized_futures_loss)
