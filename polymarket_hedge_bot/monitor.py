from dataclasses import dataclass


@dataclass(frozen=True)
class MonitorResult:
    hedge_status: str
    action: str
    realized_futures_loss: float
    current_pm_profit: float
    worst_case_hold_all: float
    allowed_remaining_pm_cost: float
    keep_fraction: float
    sell_fraction: float
    keep_shares: float
    sell_shares: float
    estimated_cash_from_sale: float
    worst_case_after_action: float
    reason: str


def monitor_position(
    pm_cost: float,
    pm_current_value: float,
    pm_shares: float,
    futures_realized_pnl: float,
    max_loss: float,
) -> MonitorResult:
    if pm_cost <= 0:
        raise ValueError("pm_cost must be positive")
    if pm_current_value < 0:
        raise ValueError("pm_current_value must be non-negative")
    if pm_shares <= 0:
        raise ValueError("pm_shares must be positive")
    if max_loss <= 0:
        raise ValueError("max_loss must be positive")

    realized_futures_loss = max(0.0, -futures_realized_pnl)
    current_pm_profit = pm_current_value - pm_cost
    worst_case_hold_all = realized_futures_loss + pm_cost
    allowed_remaining_pm_cost = max(0.0, max_loss - realized_futures_loss)

    if allowed_remaining_pm_cost <= 0:
        keep_fraction = 0.0
        action = "FULL_EXIT"
        reason = "realized futures loss already reached or exceeded max loss"
    elif worst_case_hold_all <= max_loss:
        keep_fraction = 1.0
        action = "HOLD"
        reason = "worst-case is still inside max loss"
    else:
        keep_fraction = min(1.0, allowed_remaining_pm_cost / pm_cost)
        action = "PARTIAL_EXIT"
        reason = "sell enough PM exposure so broken-hedge worst-case returns inside max loss"

    sell_fraction = 1.0 - keep_fraction
    keep_shares = pm_shares * keep_fraction
    sell_shares = pm_shares * sell_fraction
    estimated_cash_from_sale = pm_current_value * sell_fraction
    worst_case_after_action = realized_futures_loss + (pm_cost * keep_fraction)

    return MonitorResult(
        hedge_status="BROKEN" if realized_futures_loss > 0 else "ACTIVE_OR_UNKNOWN",
        action=action,
        realized_futures_loss=realized_futures_loss,
        current_pm_profit=current_pm_profit,
        worst_case_hold_all=worst_case_hold_all,
        allowed_remaining_pm_cost=allowed_remaining_pm_cost,
        keep_fraction=keep_fraction,
        sell_fraction=sell_fraction,
        keep_shares=keep_shares,
        sell_shares=sell_shares,
        estimated_cash_from_sale=estimated_cash_from_sale,
        worst_case_after_action=worst_case_after_action,
        reason=reason,
    )

