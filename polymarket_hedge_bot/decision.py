from dataclasses import dataclass

from polymarket_hedge_bot.config import RiskConfig
from polymarket_hedge_bot.edge import EdgeResult
from polymarket_hedge_bot.hedge import HedgeResult
from polymarket_hedge_bot.quality import QualityResult


@dataclass(frozen=True)
class DecisionResult:
    decision: str
    worst_case_after_sl: float
    reason: str
    post_sl_action: str


def make_decision(
    stake: float,
    edge: EdgeResult,
    hedge: HedgeResult,
    config: RiskConfig,
    sl_path_cost: float = 0.0,
    quality: QualityResult | None = None,
) -> DecisionResult:
    worst_case_after_sl = hedge.expected_sl_loss + stake + sl_path_cost

    if quality is not None and not quality.ok:
        return DecisionResult(
            decision="SKIP",
            worst_case_after_sl=worst_case_after_sl,
            reason=f"trade quality filter failed: {quality.reason}",
            post_sl_action="do not enter",
        )

    if edge.true_edge < config.watch_edge:
        return DecisionResult(
            decision="SKIP",
            worst_case_after_sl=worst_case_after_sl,
            reason="true edge is below watch threshold",
            post_sl_action="do not enter",
        )

    if worst_case_after_sl > config.max_loss_per_trade:
        return DecisionResult(
            decision="WATCH",
            worst_case_after_sl=worst_case_after_sl,
            reason="edge exists, but worst-case after SL exceeds risk limit",
            post_sl_action="reduce stake, reduce coverage, or plan partial PM exit after SL",
        )

    if edge.true_edge >= config.enter_edge:
        return DecisionResult(
            decision="ENTER",
            worst_case_after_sl=worst_case_after_sl,
            reason="edge and risk are inside configured limits",
            post_sl_action="after SL: re-hedge once, partial exit, full exit, or freeze + alert",
        )

    return DecisionResult(
        decision="WATCH",
        worst_case_after_sl=worst_case_after_sl,
        reason="edge is positive but below enter threshold",
        post_sl_action="wait for better NO price or higher distance to strike",
    )
