from dataclasses import dataclass

from polymarket_hedge_bot.config import RiskConfig
from polymarket_hedge_bot.hedge import HedgeResult


@dataclass(frozen=True)
class CostResult:
    pm_fee: float
    futures_entry_fee: float
    futures_tp_exit_fee: float
    futures_sl_exit_fee: float
    funding_cost: float
    total_cost_to_tp: float
    total_cost_to_sl: float
    pm_gross_profit_if_no_wins: float
    net_touch_with_hedge_tp: float
    net_no_win_after_hedge_sl: float
    net_touch_after_hedge_sl_loss: float


def calculate_costs(
    stake: float,
    no_price: float,
    hedge: HedgeResult,
    config: RiskConfig,
) -> CostResult:
    if stake <= 0:
        raise ValueError("stake must be positive")
    if no_price <= 0:
        raise ValueError("no_price must be positive")

    pm_fee = stake * config.pm_fee_rate
    futures_entry_fee = hedge.notional * config.futures_fee_rate
    futures_tp_exit_fee = (hedge.size_btc * hedge.take_profit) * config.futures_fee_rate
    futures_sl_exit_fee = (hedge.size_btc * hedge.stop_loss) * config.futures_fee_rate
    funding_cost = calculate_funding_cost(
        hedge.notional,
        hedge.side,
        config.funding_rate_per_period,
        config.funding_periods,
    )

    total_cost_to_tp = pm_fee + futures_entry_fee + futures_tp_exit_fee + funding_cost
    total_cost_to_sl = pm_fee + futures_entry_fee + futures_sl_exit_fee + funding_cost
    pm_gross_profit_if_no_wins = (stake / no_price) - stake

    return CostResult(
        pm_fee=pm_fee,
        futures_entry_fee=futures_entry_fee,
        futures_tp_exit_fee=futures_tp_exit_fee,
        futures_sl_exit_fee=futures_sl_exit_fee,
        funding_cost=funding_cost,
        total_cost_to_tp=total_cost_to_tp,
        total_cost_to_sl=total_cost_to_sl,
        pm_gross_profit_if_no_wins=pm_gross_profit_if_no_wins,
        net_touch_with_hedge_tp=hedge.expected_tp_profit - stake - total_cost_to_tp,
        net_no_win_after_hedge_sl=pm_gross_profit_if_no_wins - hedge.expected_sl_loss - total_cost_to_sl,
        net_touch_after_hedge_sl_loss=-(stake + hedge.expected_sl_loss + total_cost_to_sl),
    )


def calculate_funding_cost(
    notional: float,
    side: str,
    funding_rate_per_period: float,
    funding_periods: float,
) -> float:
    raw = notional * funding_rate_per_period * funding_periods
    if side.upper() == "LONG":
        return raw
    if side.upper() == "SHORT":
        return -raw
    raise ValueError("side must be LONG or SHORT")
