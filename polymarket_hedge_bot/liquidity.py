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
    limit_price: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    worst_price: float | None = None
    levels_used: int = 0
    available_cost: float = 0.0
    slippage_from_best: float | None = None
    spread: float | None = None


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


def estimate_limit_buy_opportunity(
    bids: list[OrderLevel],
    asks: list[OrderLevel],
    stake: float,
    reference_price: float,
    min_price: float = 0.40,
    max_price: float = 0.50,
    max_spread: float = 0.08,
    tick_size: float = 0.001,
) -> LiquidityCheck:
    if stake <= 0:
        raise ValueError("stake must be positive")

    usable_bids = sorted(
        (level for level in bids if level.price > 0 and level.size > 0),
        key=lambda level: level.price,
        reverse=True,
    )
    usable_asks = sorted(
        (level for level in asks if level.price > 0 and level.size > 0),
        key=lambda level: level.price,
    )
    if not usable_bids and not usable_asks:
        return LiquidityCheck(False, "orderbook has no usable bid/ask levels", requested_cost=stake)

    best_bid = usable_bids[0].price if usable_bids else None
    best_ask = usable_asks[0].price if usable_asks else None
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None

    anchor = reference_price
    if best_bid is not None and best_ask is not None:
        anchor = (best_bid + best_ask) / 2.0
    elif best_bid is not None:
        anchor = best_bid + tick_size
    elif best_ask is not None:
        anchor = min(best_ask, reference_price)

    limit_price = min(max_price, max(min_price, anchor))
    if best_ask is not None:
        limit_price = min(limit_price, max(min_price, best_ask - tick_size))
    if best_bid is not None:
        limit_price = max(limit_price, best_bid + tick_size)
    limit_price = min(max_price, max(min_price, round(limit_price, 3)))

    filled_shares = stake / limit_price
    resting_bid_cost = sum(level.price * level.size for level in usable_bids if level.price >= limit_price - 1e-9)

    if reference_price < min_price or reference_price > max_price:
        return LiquidityCheck(
            False,
            "reference NO price is outside limit-entry strategy range",
            requested_cost=stake,
            filled_shares=filled_shares,
            limit_price=limit_price,
            best_bid=best_bid,
            best_ask=best_ask,
            available_cost=resting_bid_cost,
            spread=spread,
        )

    if best_bid is not None and best_bid > max_price:
        return LiquidityCheck(
            False,
            "best bid is already above max strategy price; lower maker bid is unlikely to fill soon",
            requested_cost=stake,
            filled_shares=filled_shares,
            limit_price=limit_price,
            best_bid=best_bid,
            best_ask=best_ask,
            available_cost=resting_bid_cost,
            spread=spread,
        )

    if spread is not None and spread > max_spread:
        return LiquidityCheck(
            False,
            "orderbook spread is too wide for a realistic limit entry",
            requested_cost=stake,
            filled_shares=filled_shares,
            limit_price=limit_price,
            best_bid=best_bid,
            best_ask=best_ask,
            available_cost=resting_bid_cost,
            spread=spread,
        )

    if limit_price < min_price or limit_price > max_price:
        return LiquidityCheck(
            False,
            "suggested limit price is outside strategy range",
            requested_cost=stake,
            filled_shares=filled_shares,
            limit_price=limit_price,
            best_bid=best_bid,
            best_ask=best_ask,
            available_cost=resting_bid_cost,
            spread=spread,
        )

    return LiquidityCheck(
        True,
        "realistic NO limit order candidate",
        requested_cost=stake,
        filled_cost=stake,
        filled_shares=filled_shares,
        vwap=limit_price,
        limit_price=limit_price,
        best_bid=best_bid,
        best_ask=best_ask,
        worst_price=limit_price,
        levels_used=0,
        available_cost=resting_bid_cost,
        slippage_from_best=0.0,
        spread=spread,
    )
