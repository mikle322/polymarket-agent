import argparse
import dataclasses
import html
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from polymarket_hedge_bot.config import RiskConfig
from polymarket_hedge_bot.connectors.binance_futures import BinanceFuturesConnector
from polymarket_hedge_bot.connectors.deribit import DeribitConnector
from polymarket_hedge_bot.connectors.okx_futures import OkxFuturesConnector
from polymarket_hedge_bot.formatting import money, positive_result_probability, ua_reason
from polymarket_hedge_bot.journal import create_signal
from polymarket_hedge_bot.live_discovery import discover_polymarket_btc_candidates_with_stats
from polymarket_hedge_bot.opportunity_history import record_opportunity_history
from polymarket_hedge_bot.paper_trading import record_paper_trades, render_paper_review_summary, review_due_paper_trades
from polymarket_hedge_bot.scout import Opportunity, load_candidates, scout_candidates
from polymarket_hedge_bot.skip_journal import opportunity_key, record_skips, render_review_summary, review_due_skips
from polymarket_hedge_bot.status import format_optional_percent, now_iso, write_scanner_status, zero_reason
from polymarket_hedge_bot.telegram_bot import TelegramBot, TelegramResponse
from polymarket_hedge_bot.telegram_views import render_scout_cards
from polymarket_hedge_bot.utils import load_dotenv, safe_print


SCANNER_STATE_PATH = Path("data") / "scanner_state.json"
HEARTBEAT_STATE_KEY = "__scanner_no_signal_heartbeat__"


@dataclass(frozen=True)
class ScannerConfig:
    candidates: str
    live_polymarket: bool
    live_btc_price: float | None
    live_iv: float | None
    live_stake: float
    live_pages: int
    live_limit: int
    live_min_liquidity: float
    deribit_lookback_minutes: int
    binance_symbol: str
    okx_inst_id: str
    interval_seconds: int
    top: int
    max_loss: float
    max_futures_margin: float
    min_decision: str
    min_score: float
    min_edge: float
    min_positive_probability: float
    min_hours_to_deadline: float
    max_hours_to_deadline: float
    min_no_price: float
    max_no_price: float
    radar_enabled: bool
    radar_top: int
    radar_min_score: float
    radar_min_edge: float
    radar_min_positive_probability: float
    radar_min_hours_to_deadline: float
    radar_max_hours_to_deadline: float
    radar_min_no_price: float
    radar_max_no_price: float
    radar_min_net_upside: float
    radar_min_reward_risk: float
    cooldown_seconds: int
    live_orderbook: bool
    max_slippage: float
    pm_fee_rate: float
    futures_fee_rate: float
    funding_rate: float | None
    funding_periods: float
    min_net_upside: float
    min_reward_risk: float
    http_timeout: float
    max_workers: int
    heartbeat_seconds: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polymarket-scanner")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--candidates", default="examples/candidates.json")
    parser.add_argument("--live-polymarket", action="store_true", help="Discover live BTC candidates from Polymarket Gamma API")
    parser.add_argument("--btc-price", type=float, help="BTC spot price for live Polymarket discovery")
    parser.add_argument("--iv", type=float, help="Annualized IV for live Polymarket discovery")
    parser.add_argument("--stake", type=float, default=200.0, help="Stake for live discovered candidates")
    parser.add_argument("--live-pages", type=int, default=3)
    parser.add_argument("--live-limit", type=int, default=100)
    parser.add_argument("--live-min-liquidity", type=float, default=0.0)
    parser.add_argument("--deribit-lookback-min", type=int, default=30)
    parser.add_argument("--binance-symbol", default="BTCUSDT")
    parser.add_argument("--okx-inst-id", default="BTC-USDT-SWAP")
    parser.add_argument("--interval", type=int, default=60, help="Scan interval in seconds")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--max-loss", type=float, default=200.0)
    parser.add_argument("--max-futures-margin", type=float, default=2500.0)
    parser.add_argument("--min-decision", choices=["WATCH", "ENTER"], default="WATCH")
    parser.add_argument("--min-score", type=float, default=30.0)
    parser.add_argument("--min-edge", type=float, default=0.03)
    parser.add_argument("--min-positive-probability", type=float, default=0.50)
    parser.add_argument("--min-hours-to-deadline", type=float, default=3.0)
    parser.add_argument(
        "--max-days-to-deadline",
        type=float,
        default=35.0,
        help="Ignore markets that settle later than this many days.",
    )
    parser.add_argument("--min-no-price", type=float, default=0.01)
    parser.add_argument("--max-no-price", type=float, default=0.99)
    parser.add_argument("--no-radar", action="store_true", help="Disable soft radar candidates in scanner status")
    parser.add_argument("--radar-top", type=int, default=5)
    parser.add_argument("--radar-min-score", type=float, default=10.0)
    parser.add_argument("--radar-min-edge", type=float, default=0.0)
    parser.add_argument("--radar-min-positive-probability", type=float, default=0.50)
    parser.add_argument("--radar-min-hours-to-deadline", type=float, default=3.0)
    parser.add_argument("--radar-max-days-to-deadline", type=float, default=35.0)
    parser.add_argument("--radar-min-no-price", type=float, default=0.01)
    parser.add_argument("--radar-max-no-price", type=float, default=0.99)
    parser.add_argument("--radar-min-net-upside", type=float, default=0.0)
    parser.add_argument("--radar-min-reward-risk", type=float, default=0.0)
    parser.add_argument("--cooldown-min", type=float, default=30.0)
    parser.add_argument("--live-orderbook", action="store_true")
    parser.add_argument("--max-slippage", type=float, default=0.03)
    parser.add_argument("--pm-fee-rate", type=float, default=0.0)
    parser.add_argument("--futures-fee-rate", type=float, default=0.0005)
    parser.add_argument("--funding-rate", type=float)
    parser.add_argument("--funding-periods", type=float, default=1.0)
    parser.add_argument("--min-net-upside", type=float, default=0.0)
    parser.add_argument("--min-reward-risk", type=float, default=0.10)
    parser.add_argument("--http-timeout", type=float, default=5.0, help="HTTP timeout for public market data requests")
    parser.add_argument("--max-workers", type=int, default=8, help="Parallel workers for market pages and orderbook checks")
    parser.add_argument(
        "--heartbeat-min",
        type=float,
        default=60.0,
        help="Send a Telegram scanner summary when no signals pass filters. Set 0 to disable.",
    )
    parser.add_argument("--once", action="store_true", help="Run one scan and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts instead of sending Telegram messages")
    return parser


def config_from_args(args: argparse.Namespace) -> ScannerConfig:
    return ScannerConfig(
        candidates=args.candidates,
        live_polymarket=args.live_polymarket,
        live_btc_price=args.btc_price,
        live_iv=args.iv,
        live_stake=args.stake,
        live_pages=args.live_pages,
        live_limit=args.live_limit,
        live_min_liquidity=args.live_min_liquidity,
        deribit_lookback_minutes=args.deribit_lookback_min,
        binance_symbol=args.binance_symbol,
        okx_inst_id=args.okx_inst_id,
        interval_seconds=args.interval,
        top=args.top,
        max_loss=args.max_loss,
        max_futures_margin=args.max_futures_margin,
        min_decision=args.min_decision,
        min_score=args.min_score,
        min_edge=args.min_edge,
        min_positive_probability=args.min_positive_probability,
        min_hours_to_deadline=args.min_hours_to_deadline,
        max_hours_to_deadline=args.max_days_to_deadline * 24.0,
        min_no_price=args.min_no_price,
        max_no_price=args.max_no_price,
        radar_enabled=not args.no_radar,
        radar_top=args.radar_top,
        radar_min_score=args.radar_min_score,
        radar_min_edge=args.radar_min_edge,
        radar_min_positive_probability=args.radar_min_positive_probability,
        radar_min_hours_to_deadline=args.radar_min_hours_to_deadline,
        radar_max_hours_to_deadline=args.radar_max_days_to_deadline * 24.0,
        radar_min_no_price=args.radar_min_no_price,
        radar_max_no_price=args.radar_max_no_price,
        radar_min_net_upside=args.radar_min_net_upside,
        radar_min_reward_risk=args.radar_min_reward_risk,
        cooldown_seconds=int(args.cooldown_min * 60),
        live_orderbook=args.live_orderbook,
        max_slippage=args.max_slippage,
        pm_fee_rate=args.pm_fee_rate,
        futures_fee_rate=args.futures_fee_rate,
        funding_rate=args.funding_rate,
        funding_periods=args.funding_periods,
        min_net_upside=args.min_net_upside,
        min_reward_risk=args.min_reward_risk,
        http_timeout=args.http_timeout,
        max_workers=args.max_workers,
        heartbeat_seconds=heartbeat_seconds_from_minutes(args.heartbeat_min),
    )


def heartbeat_seconds_from_minutes(minutes: float) -> int:
    if minutes <= 0:
        return 0
    return max(1, int(minutes * 60))


def risk_config(config: ScannerConfig) -> RiskConfig:
    funding_rate = config.funding_rate if config.funding_rate is not None else 0.0
    return RiskConfig(
        max_loss_per_trade=config.max_loss,
        pm_fee_rate=config.pm_fee_rate,
        futures_fee_rate=config.futures_fee_rate,
        funding_rate_per_period=funding_rate,
        funding_periods=config.funding_periods,
        min_net_upside=config.min_net_upside,
        min_reward_risk=config.min_reward_risk,
    )


def with_live_market_inputs(config: ScannerConfig) -> tuple[ScannerConfig, dict[str, float]]:
    needs_market_data = config.live_polymarket and (config.live_btc_price is None or config.funding_rate is None)
    needs_iv = config.live_polymarket and config.live_iv is None
    timings = {
        "market_data_seconds": 0.0,
        "iv_seconds": 0.0,
    }
    if not needs_market_data and not needs_iv:
        return config, timings

    results: dict[str, ScannerConfig] = {}

    def timed_market_data() -> ScannerConfig:
        started = time.perf_counter()
        try:
            return with_live_binance_data(config)
        finally:
            timings["market_data_seconds"] = elapsed_seconds(started)

    def timed_iv() -> ScannerConfig:
        started = time.perf_counter()
        try:
            return with_live_deribit_iv(config)
        finally:
            timings["iv_seconds"] = elapsed_seconds(started)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {}
        if needs_market_data:
            futures[executor.submit(timed_market_data)] = "market"
        if needs_iv:
            futures[executor.submit(timed_iv)] = "iv"
        for future in as_completed(futures):
            results[futures[future]] = future.result()

    changes: dict[str, Any] = {}
    market_config = results.get("market")
    if market_config is not None:
        changes["live_btc_price"] = market_config.live_btc_price
        changes["funding_rate"] = market_config.funding_rate
    iv_config = results.get("iv")
    if iv_config is not None:
        changes["live_iv"] = iv_config.live_iv
    if not changes:
        return config, timings
    return dataclasses.replace(config, **changes), timings


def run_scan(config: ScannerConfig) -> list[Opportunity]:
    opportunities, effective_config, _diagnostics = evaluate_opportunities(config)
    return [opportunity for opportunity in opportunities if should_alert(opportunity, effective_config)]


def evaluate_opportunities(config: ScannerConfig) -> tuple[list[Opportunity], ScannerConfig, dict[str, Any]]:
    evaluate_started = time.perf_counter()
    timings: dict[str, float] = {}

    started = time.perf_counter()
    config, input_timings = with_live_market_inputs(config)
    timings.update(input_timings)
    timings["market_inputs_total_seconds"] = elapsed_seconds(started)

    diagnostics: dict[str, Any] = {"timings": timings}
    if config.live_polymarket:
        if config.live_btc_price is None:
            raise ValueError("--btc-price is required with --live-polymarket when Binance price is unavailable")
        if config.live_iv is None:
            raise ValueError("--iv is required with --live-polymarket")
        started = time.perf_counter()
        candidates, discovery_stats = discover_polymarket_btc_candidates_with_stats(
            stake=config.live_stake,
            btc_price=config.live_btc_price,
            iv=config.live_iv,
            limit=config.live_limit,
            pages=config.live_pages,
            min_liquidity=config.live_min_liquidity,
            timeout=config.http_timeout,
            max_workers=config.max_workers,
        )
        timings["discovery_seconds"] = elapsed_seconds(started)
        diagnostics["discovery"] = discovery_stats.to_dict()
    else:
        started = time.perf_counter()
        candidates = load_candidates(config.candidates)
        timings["candidate_load_seconds"] = elapsed_seconds(started)
        diagnostics["discovery"] = {
            "source": "candidate_file",
            "parsed_candidates": len(candidates),
        }
    all_candidates = candidates
    candidates_loaded = len(all_candidates)

    analysis_config = radar_config(config) if config.radar_enabled else config
    analysis_candidates, analysis_prefilter_stats = prefilter_candidates(all_candidates, analysis_config)
    strict_candidates, prefilter_stats = prefilter_candidates(all_candidates, config)
    strict_slugs = {candidate.slug for candidate in strict_candidates}

    started = time.perf_counter()
    opportunities, evaluation_errors = scout_candidates_safe(
        analysis_candidates,
        analysis_config,
    )
    timings["hedge_analysis_seconds"] = elapsed_seconds(started)

    strict_opportunities = [opportunity for opportunity in opportunities if opportunity.candidate.slug in strict_slugs]
    diagnostics["candidates_loaded"] = candidates_loaded
    diagnostics["prefilter"] = prefilter_stats
    diagnostics["candidates_after_prefilter"] = len(strict_candidates)
    diagnostics["analysis_prefilter"] = analysis_prefilter_stats
    diagnostics["analysis_candidates_after_prefilter"] = len(analysis_candidates)
    diagnostics["shared_analysis_opportunities"] = len(opportunities)
    diagnostics["opportunities_analyzed"] = len(strict_opportunities)
    diagnostics["evaluation_errors"] = evaluation_errors
    diagnostics["alert_rejections"] = alert_rejection_diagnostics(strict_opportunities, config)
    diagnostics["radar"] = evaluate_radar(opportunities, config, analysis_prefilter_stats, evaluation_errors)
    timings["evaluate_total_seconds"] = elapsed_seconds(evaluate_started)
    return strict_opportunities, config, diagnostics


def elapsed_seconds(started: float) -> float:
    return round(time.perf_counter() - started, 3)


def radar_config(config: ScannerConfig) -> ScannerConfig:
    return dataclasses.replace(
        config,
        min_hours_to_deadline=config.radar_min_hours_to_deadline,
        max_hours_to_deadline=config.radar_max_hours_to_deadline,
        min_no_price=config.radar_min_no_price,
        max_no_price=config.radar_max_no_price,
        min_net_upside=config.radar_min_net_upside,
        min_reward_risk=config.radar_min_reward_risk,
    )


def evaluate_radar(
    opportunities: list[Opportunity],
    config: ScannerConfig,
    prefilter_stats: dict[str, Any],
    evaluation_errors: list[dict[str, str]],
) -> dict[str, Any]:
    if not config.radar_enabled:
        return {"enabled": False}

    matched: list[Opportunity] = []
    rejected_by: dict[str, int] = {}
    for opportunity in opportunities:
        reject_reason = radar_reject_reason(opportunity, config)
        if reject_reason is None:
            matched.append(opportunity)
        else:
            rejected_by[reject_reason] = rejected_by.get(reject_reason, 0) + 1
    return {
        "enabled": True,
        "candidates_after_prefilter": len(opportunities) + len(evaluation_errors),
        "opportunities_analyzed": len(opportunities),
        "matched": len(matched),
        "rejected": len(opportunities) - len(matched),
        "rejected_by": rejected_by,
        "prefilter": prefilter_stats,
        "evaluation_errors": evaluation_errors,
        "top": [serialize_radar_opportunity(opportunity) for opportunity in matched[: config.radar_top]],
    }


def should_radar(opportunity: Opportunity, config: ScannerConfig) -> bool:
    return radar_reject_reason(opportunity, config) is None


def radar_reject_reason(opportunity: Opportunity, config: ScannerConfig) -> str | None:
    if opportunity.score < config.radar_min_score:
        return "score"
    if opportunity.edge.true_edge < config.radar_min_edge:
        return "edge"
    if positive_result_probability(opportunity.edge, opportunity.costs) < config.radar_min_positive_probability:
        return "positive_probability"
    if opportunity.quality.net_upside < config.radar_min_net_upside:
        return "net_upside"
    if opportunity.quality.reward_risk < config.radar_min_reward_risk:
        return "reward_risk"
    if not opportunity.liquidity.ok:
        return "liquidity"
    return None


def serialize_radar_opportunity(opportunity: Opportunity) -> dict[str, Any]:
    candidate = opportunity.candidate
    return {
        "slug": candidate.slug,
        "question": candidate.question,
        "deadline": candidate.deadline.isoformat(),
        "decision": opportunity.decision,
        "reason": opportunity.reason,
        "score": opportunity.score,
        "no_price": candidate.no_price,
        "positive_probability": positive_result_probability(opportunity.edge, opportunity.costs),
        "edge": opportunity.edge.true_edge,
        "fair_no": opportunity.edge.fair_no,
        "touch": opportunity.edge.fair_touch,
        "quality_label": opportunity.quality.label,
        "net_upside": opportunity.quality.net_upside,
        "reward_risk": opportunity.quality.reward_risk,
        "worst_case_after_sl": opportunity.worst_case_after_sl,
        "liquidity_reason": opportunity.liquidity.reason,
        "pm_shares": opportunity.pm_shares,
        "futures_side": opportunity.hedge.side,
        "futures_size_btc": opportunity.hedge.size_btc,
        "futures_leverage": opportunity.hedge.leverage,
        "futures_margin": opportunity.hedge.isolated_margin,
        "take_profit": opportunity.hedge.take_profit,
        "stop_loss": opportunity.hedge.stop_loss,
        "net_no_win": opportunity.costs.net_no_win_after_hedge_sl,
        "net_touch": opportunity.costs.net_touch_after_hedge_sl_loss,
    }


def prefilter_candidates(candidates: list[Any], config: ScannerConfig) -> tuple[list[Any], dict[str, Any]]:
    now = datetime.now(timezone.utc)
    kept: list[Any] = []
    deadline_filtered = 0
    deadline_too_close_filtered = 0
    deadline_too_far_filtered = 0
    no_price_filtered = 0
    examples: list[dict[str, str]] = []

    for candidate in candidates:
        hours_to_deadline = (candidate.deadline - now).total_seconds() / 3600
        if hours_to_deadline < config.min_hours_to_deadline:
            deadline_filtered += 1
            deadline_too_close_filtered += 1
            add_prefilter_example(
                examples,
                candidate,
                f"дедлайн надто близько: {hours_to_deadline:.1f}h < {config.min_hours_to_deadline:.1f}h",
            )
            continue

        if config.max_hours_to_deadline > 0 and hours_to_deadline > config.max_hours_to_deadline:
            deadline_filtered += 1
            deadline_too_far_filtered += 1
            add_prefilter_example(
                examples,
                candidate,
                f"дедлайн надто далеко: {hours_to_deadline / 24.0:.1f}d > {config.max_hours_to_deadline / 24.0:.1f}d",
            )
            continue

        if candidate.no_price < config.min_no_price or candidate.no_price > config.max_no_price:
            no_price_filtered += 1
            add_prefilter_example(
                examples,
                candidate,
                f"NO price поза діапазоном: {candidate.no_price:.3f}, потрібно {config.min_no_price:.3f}-{config.max_no_price:.3f}",
            )
            continue

        kept.append(candidate)

    return kept, {
        "deadline_filtered": deadline_filtered,
        "deadline_too_close_filtered": deadline_too_close_filtered,
        "deadline_too_far_filtered": deadline_too_far_filtered,
        "no_price_filtered": no_price_filtered,
        "examples": examples[:6],
    }


def add_prefilter_example(examples: list[dict[str, str]], candidate: Any, reason: str) -> None:
    if len(examples) >= 6:
        return
    examples.append(
        {
            "slug": str(getattr(candidate, "slug", "unknown")),
            "reason": reason,
        }
    )


def scout_candidates_safe(candidates: list, config: ScannerConfig) -> tuple[list[Opportunity], list[dict[str, str]]]:
    opportunities: list[Opportunity] = []
    errors: list[dict[str, str]] = []

    def evaluate_one(candidate: Any) -> list[Opportunity]:
        return scout_candidates(
            [candidate],
            risk_config(config),
            max_futures_margin=config.max_futures_margin,
            use_live_orderbook=config.live_orderbook,
            max_slippage=config.max_slippage,
            max_workers=1,
            polymarket_timeout=config.http_timeout,
        )

    workers = max(1, min(config.max_workers, len(candidates))) if candidates else 1
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_candidate = {executor.submit(evaluate_one, candidate): candidate for candidate in candidates}
        for future in as_completed(future_to_candidate):
            candidate = future_to_candidate[future]
            try:
                opportunities.extend(future.result())
            except Exception as exc:
                errors.append(
                    {
                        "slug": getattr(candidate, "slug", "unknown"),
                        "question": getattr(candidate, "question", ""),
                        "reason": str(exc),
                    }
                )
    return sorted(opportunities, key=lambda item: item.score, reverse=True), errors[:10]


def with_live_binance_data(config: ScannerConfig) -> ScannerConfig:
    needs_price = config.live_polymarket and config.live_btc_price is None
    needs_funding = config.live_polymarket and config.funding_rate is None
    if not needs_price and not needs_funding:
        return config

    try:
        connector = BinanceFuturesConnector(timeout=config.http_timeout)
        premium = connector.premium_index(config.binance_symbol)
        live_price = premium.mark_price
        live_funding = premium.last_funding_rate
    except (HTTPError, URLError, RuntimeError) as exc:
        safe_print(f"Binance market data unavailable, using OKX fallback: {exc}")
        try:
            okx = OkxFuturesConnector(timeout=config.http_timeout)
            ticker = okx.ticker(config.okx_inst_id)
            funding = okx.funding_rate(config.okx_inst_id)
            live_price = ticker.last
            live_funding = funding.funding_rate
        except (HTTPError, URLError, RuntimeError) as fallback_exc:
            if needs_price:
                raise RuntimeError(f"market data unavailable: {fallback_exc}") from fallback_exc
            safe_print(f"Funding unavailable, using 0.00% fallback: {fallback_exc}")
            live_price = config.live_btc_price
            live_funding = 0.0

    changes: dict[str, Any] = {}
    if needs_price:
        changes["live_btc_price"] = live_price
    if needs_funding:
        changes["funding_rate"] = live_funding
    return dataclasses.replace(config, **changes)


def with_live_deribit_iv(config: ScannerConfig) -> ScannerConfig:
    if not config.live_polymarket or config.live_iv is not None:
        return config

    try:
        connector = DeribitConnector(timeout=config.http_timeout)
        vol = connector.btc_volatility_index(config.deribit_lookback_minutes)
    except (HTTPError, URLError, RuntimeError) as exc:
        raise RuntimeError(f"Deribit IV unavailable and --iv was not provided: {exc}") from exc

    safe_print(f"Using Deribit IV: {vol.annualized_volatility * 100:.2f}%")
    return dataclasses.replace(config, live_iv=vol.annualized_volatility)


def should_alert(opportunity: Opportunity, config: ScannerConfig) -> bool:
    if decision_rank(opportunity.decision) < decision_rank(config.min_decision):
        return False
    if opportunity.score < config.min_score:
        return False
    if opportunity.edge.true_edge < config.min_edge:
        return False
    if opportunity.quality.net_upside < config.min_net_upside:
        return False
    if opportunity.quality.reward_risk < config.min_reward_risk:
        return False
    if positive_result_probability(opportunity.edge, opportunity.costs) < config.min_positive_probability:
        return False
    if not opportunity.liquidity.ok:
        return False
    return True


def decision_rank(decision: str) -> int:
    ranks = {"SKIP": 0, "WATCH": 1, "ENTER": 2}
    return ranks.get(decision, 0)


def send_alerts(
    opportunities: list[Opportunity],
    config: ScannerConfig,
    state: dict[str, Any],
    bot: TelegramBot | None,
    chat_id: str | None,
    dry_run: bool,
) -> int:
    sent = 0
    now = time.time()

    for opportunity in opportunities[: config.top]:
        key = alert_key(opportunity)
        if not should_send_again(key, opportunity, state, config, now):
            continue

        text = render_scanner_alert(opportunity)

        if dry_run:
            safe_print(text)
        else:
            signal = create_signal(
                kind="scanner",
                title=opportunity.candidate.slug,
                decision=opportunity.decision,
                positive_probability=positive_result_probability(opportunity.edge, opportunity.costs),
                payload={
                    "source": "scanner",
                    "scanner_config": asdict(config),
                    "slug": opportunity.candidate.slug,
                    "stake": opportunity.candidate.stake,
                    "decision": opportunity.decision,
                    "score": opportunity.score,
                    "edge": opportunity.edge.true_edge,
                    "positive_probability": positive_result_probability(opportunity.edge, opportunity.costs),
                    "futures_side": opportunity.hedge.side,
                    "futures_size_btc": opportunity.hedge.size_btc,
                    "futures_leverage": opportunity.hedge.leverage,
                    "worst_case_after_sl": opportunity.worst_case_after_sl,
                },
            )
            reply_markup = {"inline_keyboard": [[{"text": "Зайшов", "callback_data": f"entered:{signal.signal_id}"}]]}
            if bot is None or chat_id is None:
                raise RuntimeError("Telegram bot/chat is not configured")
            bot.send_report(chat_id, TelegramResponse(text=text, reply_markup=reply_markup, html=True))

            state[key] = {
                "sent_at": now,
                "score": opportunity.score,
                "decision": opportunity.decision,
                "edge": opportunity.edge.true_edge,
            }
        sent += 1

    return sent


def send_no_signal_heartbeat(
    opportunities: list[Opportunity],
    matched: list[Opportunity],
    config: ScannerConfig,
    diagnostics: dict[str, Any],
    state: dict[str, Any],
    bot: TelegramBot | None,
    chat_id: str | None,
    dry_run: bool,
) -> bool:
    if matched or config.heartbeat_seconds <= 0:
        return False

    now = time.time()
    previous = state.get(HEARTBEAT_STATE_KEY) or {}
    age = now - float(previous.get("sent_at", 0))
    reason = zero_reason(diagnostics)
    signature = heartbeat_signature(diagnostics, reason)
    signature_changed = signature != previous.get("signature")
    if age < config.heartbeat_seconds and not signature_changed:
        return False

    text = render_no_signal_heartbeat(opportunities, config, diagnostics, reason)
    if dry_run:
        safe_print(text)
    else:
        if bot is None or chat_id is None:
            raise RuntimeError("Telegram bot/chat is not configured")
        bot.send_report(chat_id, TelegramResponse(text=text, html=True))

    state[HEARTBEAT_STATE_KEY] = {
        "sent_at": now,
        "signature": signature,
    }
    return True


def heartbeat_signature(diagnostics: dict[str, Any], reason: str) -> str:
    radar = (diagnostics.get("radar") or {}).get("rejected_by") or {}
    prefilter = diagnostics.get("prefilter") or {}
    alert_rejections = diagnostics.get("alert_rejections") or []
    first_alert_rejection = alert_rejections[0] if alert_rejections else {}
    return ":".join(
        [
            reason,
            str(diagnostics.get("candidates_loaded", 0)),
            str(diagnostics.get("candidates_after_prefilter", 0)),
            str(diagnostics.get("opportunities_analyzed", 0)),
            str(prefilter.get("deadline_filtered", 0)),
            str(prefilter.get("no_price_filtered", 0)),
            str(radar.get("score", 0)),
            str(radar.get("edge", 0)),
            str(radar.get("net_upside", 0)),
            str(radar.get("reward_risk", 0)),
            str(first_alert_rejection.get("slug", "")),
            "|".join(str(item) for item in first_alert_rejection.get("failures", [])),
        ]
    )


def render_no_signal_heartbeat(
    opportunities: list[Opportunity],
    config: ScannerConfig,
    diagnostics: dict[str, Any],
    reason: str,
) -> str:
    radar = diagnostics.get("radar") or {}
    prefilter = diagnostics.get("prefilter") or {}
    alert_rejections = diagnostics.get("alert_rejections") or []
    reject_counts = count_alert_rejections(opportunities, config)

    lines = [
        "<b>🟡 Scanner працює, сигналів немає</b>",
        "",
        f"• Кандидатів знайдено: <b>{diagnostics.get('candidates_loaded', 'n/a')}</b>",
        f"• Після pre-filter: <b>{diagnostics.get('candidates_after_prefilter', 'n/a')}</b>",
        f"• Проаналізовано: <b>{diagnostics.get('opportunities_analyzed', len(opportunities))}</b>",
        "• Пройшли alert-фільтри: <b>0</b>",
        f"• BTC: <b>{format_number(config.live_btc_price)}</b> | IV: <b>{format_optional_percent(config.live_iv)}</b>",
        f"• Причина: {esc(reason or 'угоди є, але зараз не проходять фільтри якості.')}",
    ]

    if prefilter:
        lines.extend(
            [
                "",
                "<b>Pre-filter</b>",
                f"• Дедлайн: <b>{prefilter.get('deadline_filtered', 0)}</b> "
                f"(близько {prefilter.get('deadline_too_close_filtered', 0)}, далеко {prefilter.get('deadline_too_far_filtered', 0)})",
                f"• NO price: <b>{prefilter.get('no_price_filtered', 0)}</b>",
            ]
        )

    if reject_counts:
        lines.extend(["", "<b>Alert-фільтри</b>"])
        for label, key in alert_reject_labels():
            if reject_counts.get(key, 0):
                lines.append(f"• {label}: <b>{reject_counts[key]}</b>")

    if alert_rejections:
        lines.extend(["", "<b>Точні причини по найближчих угодах</b>"])
        for index, item in enumerate(alert_rejections[:5], start=1):
            lines.extend(
                [
                    f"{index}. <code>{esc(item.get('slug', 'unknown'))}</code>",
                    f"• Етап: <b>{esc(item.get('stage', 'unknown'))}</b>",
                    f"• Decision: <b>{esc(item.get('decision', 'n/a'))}</b> | score <b>{float(item.get('score', 0.0)):.1f}</b>",
                    f"• Причина моделі: {esc(item.get('reason', 'unknown'))}",
                ]
            )
            for failure in (item.get("failures") or [])[:6]:
                lines.append(f"  - {esc(failure)}")

    if radar.get("enabled"):
        rejected_by = radar.get("rejected_by") or {}
        lines.extend(
            [
                "",
                "<b>Radar</b>",
                f"• Пройшли radar: <b>{radar.get('matched', 0)}</b>",
                f"• Відсіяно radar: <b>{radar.get('rejected', 0)}</b>",
            ]
        )
        if rejected_by:
            short_reasons = ", ".join(f"{key}: {value}" for key, value in rejected_by.items() if value)
            lines.append(f"• Причини: <code>{esc(short_reasons)}</code>")

    if opportunities:
        best = opportunities[0]
        lines.extend(
            [
                "",
                "<b>Найближчий до сигналу</b>",
                f"<code>{esc(best.candidate.slug)}</code>",
                f"• Decision: <b>{esc(best.decision)}</b> | score <b>{best.score:.1f}</b>",
                f"• Edge: <b>{best.edge.true_edge * 100:.1f}%</b> | NO <b>{best.edge.no_price:.3f}</b>",
                f"• Net upside: <b>{money(best.quality.net_upside)}</b> | R/R <b>{best.quality.reward_risk:.2f}</b>",
                f"• Причина: {esc(ua_reason(best.reason))}",
            ]
        )

    lines.extend(["", "Детальніше: /status або /radar"])
    return "\n".join(lines)


def count_alert_rejections(opportunities: list[Opportunity], config: ScannerConfig) -> dict[str, int]:
    counts: dict[str, int] = {}
    for opportunity in opportunities:
        reason = alert_reject_reason(opportunity, config)
        if reason is None:
            continue
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def alert_rejection_diagnostics(
    opportunities: list[Opportunity],
    config: ScannerConfig,
    limit: int = 5,
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for opportunity in opportunities:
        failures = alert_reject_details(opportunity, config)
        if not failures:
            continue
        diagnostics.append(
            {
                "stage": "Alert-фільтри",
                "slug": opportunity.candidate.slug,
                "decision": opportunity.decision,
                "score": opportunity.score,
                "reason": ua_reason(opportunity.reason),
                "failures": failures,
            }
        )
        if len(diagnostics) >= limit:
            break
    return diagnostics


def alert_reject_details(opportunity: Opportunity, config: ScannerConfig) -> list[str]:
    failures: list[str] = []
    if decision_rank(opportunity.decision) < decision_rank(config.min_decision):
        failures.append(f"Decision {opportunity.decision} < {config.min_decision}")
    if opportunity.score < config.min_score:
        failures.append(f"Score {opportunity.score:.1f} < {config.min_score:.1f}")
    if opportunity.edge.true_edge < config.min_edge:
        failures.append(f"Edge {opportunity.edge.true_edge * 100:.1f}% < {config.min_edge * 100:.1f}%")
    if opportunity.quality.net_upside < config.min_net_upside:
        failures.append(f"Net upside {money(opportunity.quality.net_upside)} < {money(config.min_net_upside)}")
    if opportunity.quality.reward_risk < config.min_reward_risk:
        failures.append(f"Reward/Risk {opportunity.quality.reward_risk:.2f} < {config.min_reward_risk:.2f}")
    positive_probability = positive_result_probability(opportunity.edge, opportunity.costs)
    if positive_probability < config.min_positive_probability:
        failures.append(
            f"NO win probability {positive_probability * 100:.1f}% < {config.min_positive_probability * 100:.1f}%"
        )
    if not opportunity.liquidity.ok:
        failures.append(f"Liquidity: {opportunity.liquidity.reason}")
    return failures


def alert_reject_reason(opportunity: Opportunity, config: ScannerConfig) -> str | None:
    if decision_rank(opportunity.decision) < decision_rank(config.min_decision):
        return "decision"
    if opportunity.score < config.min_score:
        return "score"
    if opportunity.edge.true_edge < config.min_edge:
        return "edge"
    if opportunity.quality.net_upside < config.min_net_upside:
        return "net_upside"
    if opportunity.quality.reward_risk < config.min_reward_risk:
        return "reward_risk"
    if positive_result_probability(opportunity.edge, opportunity.costs) < config.min_positive_probability:
        return "positive_probability"
    if not opportunity.liquidity.ok:
        return "liquidity"
    return None


def alert_reject_labels() -> list[tuple[str, str]]:
    return [
        ("Decision нижче порогу", "decision"),
        ("Score", "score"),
        ("Edge", "edge"),
        ("Net upside", "net_upside"),
        ("Reward/Risk", "reward_risk"),
        ("NO wins probability", "positive_probability"),
        ("Liquidity", "liquidity"),
    ]


def format_number(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return esc(value)


def esc(value: Any) -> str:
    return html.escape(str(value))


def render_scanner_alert(opportunity: Opportunity) -> str:
    body = render_scout_cards([opportunity], top=1)
    if opportunity.decision == "ENTER":
        title = "🚨 Новий ENTER-сигнал від 24/7 scanner"
    else:
        title = "🟡 Новий potential setup від 24/7 scanner"
    return f"<b>{title}</b>\n\n" + body


def alert_key(opportunity: Opportunity) -> str:
    return f"{opportunity.candidate.slug}:{opportunity.decision}"


def should_send_again(
    key: str,
    opportunity: Opportunity,
    state: dict[str, Any],
    config: ScannerConfig,
    now: float,
) -> bool:
    previous = state.get(key)
    if previous is None:
        return True

    age = now - float(previous.get("sent_at", 0))
    score_delta = opportunity.score - float(previous.get("score", 0))
    decision_changed = opportunity.decision != previous.get("decision")

    if decision_changed:
        return True
    if age >= config.cooldown_seconds:
        return True
    if score_delta >= 10:
        return True
    return False


def load_state() -> dict[str, Any]:
    if not SCANNER_STATE_PATH.exists():
        return {}
    return json.loads(SCANNER_STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    SCANNER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCANNER_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def cleanup_state(state: dict[str, Any], max_age_seconds: float = 7 * 24 * 3600) -> int:
    now = time.time()
    stale = [key for key, entry in state.items() if now - float(entry.get("sent_at", 0)) > max_age_seconds]
    for key in stale:
        del state[key]
    return len(stale)


def build_telegram_bot(dry_run: bool) -> tuple[TelegramBot | None, str | None]:
    if dry_run:
        return None, None

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing")
    if not chat_id:
        raise SystemExit("TELEGRAM_ALLOWED_CHAT_ID is missing")
    return TelegramBot(token=token, allowed_chat_id=chat_id), chat_id


def run_scanner_loop(
    config: ScannerConfig,
    bot: TelegramBot | None,
    chat_id: str | None,
    dry_run: bool = False,
    once: bool = False,
    stop_event: threading.Event | None = None,
) -> int:
    safe_print("24/7 scanner started")
    safe_print(f"Source: {'live Polymarket' if config.live_polymarket else config.candidates}")
    safe_print(f"Interval: {config.interval_seconds}s")
    safe_print(
        "Filters: "
        f"min_decision={config.min_decision}, "
        f"min_score={config.min_score}, "
        f"min_edge={config.min_edge}, "
        f"min_hours_to_deadline={config.min_hours_to_deadline}, "
        f"max_days_to_deadline={config.max_hours_to_deadline / 24.0:.1f}, "
        f"no_price={config.min_no_price}-{config.max_no_price}"
    )

    while stop_event is None or not stop_event.is_set():
        started_at = now_iso()
        scan_started = time.perf_counter()
        state = load_state()
        try:
            opportunities, effective_config, diagnostics = evaluate_opportunities(config)
            matched = [opportunity for opportunity in opportunities if should_alert(opportunity, effective_config)]
            matched_keys = {opportunity_key(opportunity) for opportunity in matched}
            skipped_logged = record_skips(opportunities, matched_keys)
            sent = send_alerts(matched, effective_config, state, bot, chat_id, dry_run)
            diagnostics["matched_alert_filters"] = len(matched)
            diagnostics["sent_after_cooldown"] = sent
            diagnostics["skipped_logged"] = skipped_logged
            history_logged = record_opportunity_history(opportunities, matched_keys, diagnostics)
            diagnostics["opportunity_history_logged"] = history_logged
            paper_logged = record_paper_trades(matched) if not dry_run else 0
            diagnostics["paper_trades_logged"] = paper_logged
            heartbeat_sent = send_no_signal_heartbeat(
                opportunities,
                matched,
                effective_config,
                diagnostics,
                state,
                bot,
                chat_id,
                dry_run,
            )
            diagnostics["no_signal_heartbeat_sent"] = heartbeat_sent
            review_summary = review_due_skips(limit=10) if not dry_run else None
            if review_summary is not None and review_summary.reviewed > 0:
                review_text = render_review_summary(review_summary)
                safe_print(review_text)
                if bot is not None and chat_id is not None:
                    bot.send_report(chat_id, TelegramResponse(text=review_text, html=True))
            paper_review = review_due_paper_trades(limit=10) if not dry_run else None
            if paper_review is not None and paper_review.reviewed > 0:
                paper_review_text = render_paper_review_summary(paper_review)
                safe_print(paper_review_text)
                if bot is not None and chat_id is not None:
                    bot.send_report(chat_id, TelegramResponse(text=paper_review_text, html=True))
            if not dry_run:
                cleaned = cleanup_state(state)
                if cleaned:
                    safe_print(f"Cleaned {cleaned} stale state entries")
                save_state(state)
            diagnostics.setdefault("timings", {})["scan_loop_seconds"] = elapsed_seconds(scan_started)
            write_scan_status(
                config=effective_config,
                started_at=started_at,
                ok=True,
                scanned=len(opportunities),
                matched=len(matched),
                sent=sent,
                skipped_logged=skipped_logged,
                diagnostics=diagnostics,
            )
            safe_print(f"Scan done: matched={len(matched)}, sent={sent}, skipped_logged={skipped_logged}")
        except Exception as exc:
            write_scan_status(
                config=config,
                started_at=started_at,
                ok=False,
                scanned=0,
                matched=0,
                sent=0,
                skipped_logged=0,
                error=str(exc),
            )
            safe_print(f"Scanner error: {exc}")

        if once:
            return 0
        if stop_event is None:
            time.sleep(config.interval_seconds)
        else:
            stop_event.wait(config.interval_seconds)

    safe_print("24/7 scanner stopped")
    return 0


def write_scan_status(
    config: ScannerConfig,
    started_at: str,
    ok: bool,
    scanned: int,
    matched: int,
    sent: int,
    skipped_logged: int,
    diagnostics: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    write_scanner_status(
        {
            "ok": ok,
            "started_at": started_at,
            "finished_at": now_iso(),
            "source": "live Polymarket" if config.live_polymarket else config.candidates,
            "interval_seconds": config.interval_seconds,
            "scanned": scanned,
            "matched": matched,
            "sent": sent,
            "skipped_logged": skipped_logged,
            "btc_price": config.live_btc_price,
            "iv": config.live_iv,
            "funding_rate": config.funding_rate,
            "min_decision": config.min_decision,
            "min_score": config.min_score,
            "min_edge": config.min_edge,
            "min_positive_probability": config.min_positive_probability,
            "min_hours_to_deadline": config.min_hours_to_deadline,
            "max_hours_to_deadline": config.max_hours_to_deadline,
            "min_no_price": config.min_no_price,
            "max_no_price": config.max_no_price,
            "radar_enabled": config.radar_enabled,
            "radar_top": config.radar_top,
            "radar_min_score": config.radar_min_score,
            "radar_min_edge": config.radar_min_edge,
            "radar_min_positive_probability": config.radar_min_positive_probability,
            "radar_min_hours_to_deadline": config.radar_min_hours_to_deadline,
            "radar_max_hours_to_deadline": config.radar_max_hours_to_deadline,
            "radar_min_no_price": config.radar_min_no_price,
            "radar_max_no_price": config.radar_max_no_price,
            "radar_min_net_upside": config.radar_min_net_upside,
            "radar_min_reward_risk": config.radar_min_reward_risk,
            "min_net_upside": config.min_net_upside,
            "min_reward_risk": config.min_reward_risk,
            "live_orderbook": config.live_orderbook,
            "http_timeout": config.http_timeout,
            "max_workers": config.max_workers,
            "heartbeat_seconds": config.heartbeat_seconds,
            "diagnostics": diagnostics or {},
            "error": error,
        }
    )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    load_dotenv(args.env_file)
    config = config_from_args(args)
    bot, chat_id = build_telegram_bot(args.dry_run)
    return run_scanner_loop(config, bot, chat_id, dry_run=args.dry_run, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
