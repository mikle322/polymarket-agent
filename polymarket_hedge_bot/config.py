from dataclasses import dataclass


@dataclass(frozen=True)
class RiskConfig:
    max_loss_per_trade: float = 200.0
    default_coverage: float = 0.60
    max_coverage: float = 0.70
    tp_buffer: float = 100.0
    min_sl_distance: float = 2000.0
    max_leverage: float = 10.0
    slippage: float = 0.02
    funding_buffer: float = 0.01
    basis_buffer: float = 0.01
    execution_buffer: float = 0.01
    pm_fee_rate: float = 0.0
    futures_fee_rate: float = 0.0005
    funding_rate_per_period: float = 0.0
    funding_periods: float = 0.0
    min_net_upside: float = 30.0
    min_reward_risk: float = 0.25
    enter_edge: float = 0.10
    watch_edge: float = 0.05
