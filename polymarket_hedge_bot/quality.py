from dataclasses import dataclass

from polymarket_hedge_bot.costs import CostResult


@dataclass(frozen=True)
class QualityResult:
    net_upside: float
    worst_downside: float
    reward_risk: float
    ok: bool
    label: str
    reason: str


def calculate_quality(
    costs: CostResult,
    min_net_upside: float = 30.0,
    min_reward_risk: float = 0.25,
) -> QualityResult:
    net_upside = max(costs.net_no_win_after_hedge_sl, costs.pm_gross_profit_if_no_wins - costs.total_cost_to_tp)
    worst_downside = abs(min(costs.net_touch_after_hedge_sl_loss, costs.net_touch_with_hedge_tp, 0.0))
    reward_risk = net_upside / worst_downside if worst_downside > 0 else float("inf")

    if net_upside < min_net_upside:
        return QualityResult(
            net_upside=net_upside,
            worst_downside=worst_downside,
            reward_risk=reward_risk,
            ok=False,
            label="погана",
            reason=f"net upside замалий: ${net_upside:.2f}",
        )

    if reward_risk < min_reward_risk:
        return QualityResult(
            net_upside=net_upside,
            worst_downside=worst_downside,
            reward_risk=reward_risk,
            ok=False,
            label="погана",
            reason=f"reward/risk замалий: {reward_risk:.2f}",
        )

    if reward_risk >= 0.75:
        label = "сильна"
    elif reward_risk >= 0.40:
        label = "нормальна"
    else:
        label = "слабка"

    return QualityResult(
        net_upside=net_upside,
        worst_downside=worst_downside,
        reward_risk=reward_risk,
        ok=True,
        label=label,
        reason="net upside і reward/risk проходять мінімальні фільтри",
    )

