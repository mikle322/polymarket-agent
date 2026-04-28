from dataclasses import dataclass


@dataclass(frozen=True)
class OrderLevel:
    price: float
    size: float


@dataclass(frozen=True)
class LiquidityCheck:
    ok: bool
    reason: str
    requested_cost: float = 0.0
    filled_cost: float = 0.0
    filled_shares: float = 0.0
    vwap: float | None = None
    best_ask: float | None = None
    worst_price: float | None = None
    levels_used: int = 0
    available_cost: float = 0.0
    slippage_from_best: float | None = None


def check_basic_liquidity(spread: float | None, available_liquidity: float | None, stake: float) -> LiquidityCheck:
    if spread is not None and spread > 0.08:
        return LiquidityCheck(False, "Polymarket spread is wider than 8c")
    if available_liquidity is not None and available_liquidity < stake * 3:
        return LiquidityCheck(False, "Polymarket liquidity is below 3x stake")
    return LiquidityCheck(True, "basic liquidity checks passed")


def estimate_buy_from_asks(
    asks: list[OrderLevel],
    stake: float,
    max_vwap: float | None = None,
    max_slippage: float | None = 0.03,
) -> LiquidityCheck:
    if stake <= 0:
        raise ValueError("stake must be positive")
    if not asks:
        return LiquidityCheck(False, "orderbook has no asks", requested_cost=stake)

    sorted_asks = sorted(asks, key=lambda level: level.price)
    best_ask = sorted_asks[0].price
    remaining = stake
    filled_cost = 0.0
    filled_shares = 0.0
    levels_used = 0
    worst_price = best_ask

    for level in sorted_asks:
        if level.price <= 0 or level.size <= 0:
            continue
        level_cost = level.price * level.size
        if level_cost <= 0:
            continue

        levels_used += 1
        worst_price = level.price
        spend = min(remaining, level_cost)
        filled_cost += spend
        filled_shares += spend / level.price
        remaining -= spend
        if remaining <= 1e-9:
            break

    if filled_shares <= 0:
        return LiquidityCheck(False, "no usable ask liquidity", requested_cost=stake, best_ask=best_ask)

    vwap = filled_cost / filled_shares
    slippage_from_best = vwap - best_ask
    available_cost = sum(max(0.0, level.price * level.size) for level in sorted_asks)

    if filled_cost + 1e-9 < stake:
        return LiquidityCheck(
            False,
            "not enough ask liquidity to fill intended stake",
            requested_cost=stake,
            filled_cost=filled_cost,
            filled_shares=filled_shares,
            vwap=vwap,
            best_ask=best_ask,
            worst_price=worst_price,
            levels_used=levels_used,
            available_cost=available_cost,
            slippage_from_best=slippage_from_best,
        )

    if max_vwap is not None and vwap > max_vwap:
        return LiquidityCheck(
            False,
            "VWAP is above max acceptable NO price",
            requested_cost=stake,
            filled_cost=filled_cost,
            filled_shares=filled_shares,
            vwap=vwap,
            best_ask=best_ask,
            worst_price=worst_price,
            levels_used=levels_used,
            available_cost=available_cost,
            slippage_from_best=slippage_from_best,
        )

    if max_slippage is not None and slippage_from_best > max_slippage:
        return LiquidityCheck(
            False,
            "orderbook slippage is too high for intended stake",
            requested_cost=stake,
            filled_cost=filled_cost,
            filled_shares=filled_shares,
            vwap=vwap,
            best_ask=best_ask,
            worst_price=worst_price,
            levels_used=levels_used,
            available_cost=available_cost,
            slippage_from_best=slippage_from_best,
        )

    return LiquidityCheck(
        True,
        "enough ask liquidity for intended stake",
        requested_cost=stake,
        filled_cost=filled_cost,
        filled_shares=filled_shares,
        vwap=vwap,
        best_ask=best_ask,
        worst_price=worst_price,
        levels_used=levels_used,
        available_cost=available_cost,
        slippage_from_best=slippage_from_best,
    )
