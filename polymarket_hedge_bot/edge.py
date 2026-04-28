from dataclasses import dataclass

from polymarket_hedge_bot.config import RiskConfig


@dataclass(frozen=True)
class EdgeResult:
    fair_touch: float
    fair_no: float
    no_price: float
    total_buffer: float
    true_edge: float


def calculate_edge(fair_touch: float, no_price: float, config: RiskConfig) -> EdgeResult:
    if not 0 <= fair_touch <= 1:
        raise ValueError("fair_touch must be between 0 and 1")
    if not 0 <= no_price <= 1:
        raise ValueError("no_price must be between 0 and 1")

    fair_no = 1.0 - fair_touch
    total_buffer = (
        config.pm_fee
        + config.slippage
        + config.funding_buffer
        + config.basis_buffer
        + config.execution_buffer
    )
    true_edge = fair_no - no_price - total_buffer
    return EdgeResult(
        fair_touch=fair_touch,
        fair_no=fair_no,
        no_price=no_price,
        total_buffer=total_buffer,
        true_edge=true_edge,
    )

