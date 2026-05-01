import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_hedge_bot.config import RiskConfig
from polymarket_hedge_bot.connectors.polymarket import PolymarketConnector
from polymarket_hedge_bot.costs import CostResult, calculate_costs
from polymarket_hedge_bot.decision import make_decision
from polymarket_hedge_bot.edge import EdgeResult, calculate_edge
from polymarket_hedge_bot.hedge import HedgeResult, calculate_futures_hedge
from polymarket_hedge_bot.liquidity import LiquidityCheck, check_basic_liquidity, estimate_limit_buy_opportunity
from polymarket_hedge_bot.probability import touch_probability, years_until
from polymarket_hedge_bot.quality import QualityResult, calculate_quality


@dataclass(frozen=True)
class CandidateMarket:
    slug: str
    question: str
    strike: float
    direction: str
    deadline: datetime
    btc_price: float
    iv: float
    no_price: float
    stake: float
    spread: float | None = None
    liquidity: float | None = None
    no_token_id: str | None = None
    market_type: str = "touch"


@dataclass(frozen=True)
class Opportunity:
    candidate: CandidateMarket
    edge: EdgeResult
    hedge: HedgeResult
    liquidity: LiquidityCheck
    costs: CostResult
    quality: QualityResult
    decision: str
    reason: str
    post_sl_action: str
    pm_shares: float
    worst_case_after_sl: float
    risk_ratio: float
    score: float


def parse_deadline(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_candidates(path: str | Path, default_stake: float | None = None) -> list[CandidateMarket]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("candidate file must contain a JSON list")

    candidates: list[CandidateMarket] = []
    for item in raw:
        candidates.append(_candidate_from_dict(item, default_stake))
    return candidates


def _candidate_from_dict(item: dict[str, Any], default_stake: float | None) -> CandidateMarket:
    stake = float(item.get("stake", default_stake or 200.0))
    return CandidateMarket(
        slug=str(item["slug"]),
        question=str(item.get("question", item["slug"])),
        strike=float(item["strike"]),
        direction=str(item["direction"]),
        deadline=parse_deadline(str(item["deadline"])),
        btc_price=float(item["btc_price"]),
        iv=float(item["iv"]),
        no_price=float(item["no_price"]),
        stake=stake,
        spread=_optional_float(item.get("spread")),
        liquidity=_optional_float(item.get("liquidity")),
        no_token_id=item.get("no_token_id"),
        market_type=str(item.get("market_type", "touch")),
    )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def evaluate_candidate(
    candidate: CandidateMarket,
    config: RiskConfig,
    max_futures_margin: float | None = None,
    polymarket: PolymarketConnector | None = None,
    use_live_orderbook: bool = False,
    max_slippage: float | None = 0.03,
    min_limit_price: float = 0.40,
    max_limit_price: float = 0.60,
) -> Opportunity:
    t = years_until(candidate.deadline)
    fair_touch = touch_probability(candidate.btc_price, candidate.strike, candidate.iv, t, candidate.direction)
    liquidity = _check_liquidity(
        candidate,
        polymarket,
        use_live_orderbook,
        max_slippage,
        min_limit_price,
        max_limit_price,
    )
    no_price = liquidity.limit_price if liquidity.limit_price is not None else candidate.no_price
    edge = calculate_edge(fair_touch, no_price, config)
    hedge = calculate_futures_hedge(
        pm_invested=candidate.stake,
        btc_entry=candidate.btc_price,
        strike=candidate.strike,
        direction=candidate.direction,
        config=config,
        max_futures_margin=max_futures_margin,
    )
    costs = calculate_costs(candidate.stake, no_price, hedge, config)
    quality = calculate_quality(costs, config.min_net_upside, config.min_reward_risk)
    decision = make_decision(candidate.stake, edge, hedge, config, sl_path_cost=costs.total_cost_to_sl, quality=quality)

    final_decision = decision.decision
    reason = decision.reason
    if not liquidity.ok:
        final_decision = "SKIP"
        reason = liquidity.reason

    pm_shares = liquidity.filled_shares if liquidity.filled_shares > 0 else candidate.stake / no_price
    risk_ratio = decision.worst_case_after_sl / config.max_loss_per_trade
    score = score_opportunity(final_decision, edge.true_edge, risk_ratio, liquidity.ok)

    return Opportunity(
        candidate=candidate,
        edge=edge,
        hedge=hedge,
        liquidity=liquidity,
        costs=costs,
        quality=quality,
        decision=final_decision,
        reason=reason,
        post_sl_action=decision.post_sl_action,
        pm_shares=pm_shares,
        worst_case_after_sl=decision.worst_case_after_sl,
        risk_ratio=risk_ratio,
        score=score,
    )


def score_opportunity(decision: str, true_edge: float, risk_ratio: float, liquidity_ok: bool) -> float:
    decision_bonus = {"ENTER": 100.0, "WATCH": 50.0, "SKIP": 0.0}.get(decision, 0.0)
    liquidity_penalty = 0.0 if liquidity_ok else 50.0
    risk_penalty = max(0.0, risk_ratio - 1.0) * 25.0
    return decision_bonus + (true_edge * 100.0) - risk_penalty - liquidity_penalty


def scout_candidates(
    candidates: list[CandidateMarket],
    config: RiskConfig,
    max_futures_margin: float | None = None,
    use_live_orderbook: bool = False,
    max_slippage: float | None = 0.03,
    min_limit_price: float = 0.40,
    max_limit_price: float = 0.60,
    max_workers: int = 8,
    polymarket_timeout: float = 5.0,
) -> list[Opportunity]:
    def evaluate(candidate: CandidateMarket) -> Opportunity:
        polymarket = PolymarketConnector(timeout=polymarket_timeout) if use_live_orderbook else None
        return evaluate_candidate(
            candidate,
            config,
            max_futures_margin,
            polymarket=polymarket,
            use_live_orderbook=use_live_orderbook,
            max_slippage=max_slippage,
            min_limit_price=min_limit_price,
            max_limit_price=max_limit_price,
        )

    if use_live_orderbook and len(candidates) > 1:
        workers = max(1, min(max_workers, len(candidates)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            opportunities = list(executor.map(evaluate, candidates))
    else:
        opportunities = [evaluate(candidate) for candidate in candidates]
    return sorted(opportunities, key=lambda item: item.score, reverse=True)


def _check_liquidity(
    candidate: CandidateMarket,
    polymarket: PolymarketConnector | None,
    use_live_orderbook: bool,
    max_slippage: float | None,
    min_limit_price: float,
    max_limit_price: float,
) -> LiquidityCheck:
    if use_live_orderbook:
        if polymarket is None:
            raise ValueError("Polymarket connector is required for live orderbook checks")
        if not candidate.no_token_id:
            return LiquidityCheck(False, "live orderbook requested, but candidate has no no_token_id")
        book = polymarket.get_orderbook(candidate.no_token_id)
        max_spread = max(0.08, max_slippage) if max_slippage is not None else 0.08
        return estimate_limit_buy_opportunity(
            book.bids,
            book.asks,
            candidate.stake,
            reference_price=candidate.no_price,
            min_price=min_limit_price,
            max_price=max_limit_price,
            max_spread=max_spread,
            tick_size=book.tick_size or 0.001,
        )
    return check_basic_liquidity(candidate.spread, candidate.liquidity, candidate.stake)
