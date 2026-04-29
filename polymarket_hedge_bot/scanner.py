import argparse
import dataclasses
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from polymarket_hedge_bot.config import RiskConfig
from polymarket_hedge_bot.connectors.binance_futures import BinanceFuturesConnector
from polymarket_hedge_bot.connectors.deribit import DeribitConnector
from polymarket_hedge_bot.connectors.okx_futures import OkxFuturesConnector
from polymarket_hedge_bot.formatting import positive_result_probability
from polymarket_hedge_bot.journal import create_signal
from polymarket_hedge_bot.live_discovery import discover_polymarket_btc_candidates_with_stats
from polymarket_hedge_bot.scout import Opportunity, load_candidates, scout_candidates
from polymarket_hedge_bot.skip_journal import opportunity_key, record_skips, render_review_summary, review_due_skips
from polymarket_hedge_bot.status import now_iso, write_scanner_status
from polymarket_hedge_bot.telegram_bot import TelegramBot, TelegramResponse
from polymarket_hedge_bot.telegram_views import render_scout_cards
from polymarket_hedge_bot.utils import load_dotenv, safe_print


SCANNER_STATE_PATH = Path("data") / "scanner_state.json"


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
    min_no_price: float
    max_no_price: float
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
    parser.add_argument("--min-score", type=float, default=60.0)
    parser.add_argument("--min-edge", type=float, default=0.10)
    parser.add_argument("--min-positive-probability", type=float, default=0.60)
    parser.add_argument("--min-hours-to-deadline", type=float, default=6.0)
    parser.add_argument("--min-no-price", type=float, default=0.05)
    parser.add_argument("--max-no-price", type=float, default=0.90)
    parser.add_argument("--cooldown-min", type=float, default=30.0)
    parser.add_argument("--live-orderbook", action="store_true")
    parser.add_argument("--max-slippage", type=float, default=0.03)
    parser.add_argument("--pm-fee-rate", type=float, default=0.0)
    parser.add_argument("--futures-fee-rate", type=float, default=0.0005)
    parser.add_argument("--funding-rate", type=float)
    parser.add_argument("--funding-periods", type=float, default=1.0)
    parser.add_argument("--min-net-upside", type=float, default=30.0)
    parser.add_argument("--min-reward-risk", type=float, default=0.25)
    parser.add_argument("--http-timeout", type=float, default=5.0, help="HTTP timeout for public market data requests")
    parser.add_argument("--max-workers", type=int, default=8, help="Parallel workers for market pages and orderbook checks")
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
        min_no_price=args.min_no_price,
        max_no_price=args.max_no_price,
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
    )


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


def run_scan(config: ScannerConfig) -> list[Opportunity]:
    opportunities, effective_config, _diagnostics = evaluate_opportunities(config)
    return [opportunity for opportunity in opportunities if should_alert(opportunity, effective_config)]


def evaluate_opportunities(config: ScannerConfig) -> tuple[list[Opportunity], ScannerConfig, dict[str, Any]]:
    config = with_live_binance_data(config)
    config = with_live_deribit_iv(config)
    diagnostics: dict[str, Any] = {}
    if config.live_polymarket:
        if config.live_btc_price is None:
            raise ValueError("--btc-price is required with --live-polymarket when Binance price is unavailable")
        if config.live_iv is None:
            raise ValueError("--iv is required with --live-polymarket")
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
        diagnostics["discovery"] = discovery_stats.to_dict()
    else:
        candidates = load_candidates(config.candidates)
        diagnostics["discovery"] = {
            "source": "candidate_file",
            "parsed_candidates": len(candidates),
        }
    candidates_loaded = len(candidates)
    candidates, prefilter_stats = prefilter_candidates(candidates, config)
    opportunities, evaluation_errors = scout_candidates_safe(
        candidates,
        config,
    )
    diagnostics["candidates_loaded"] = candidates_loaded
    diagnostics["prefilter"] = prefilter_stats
    diagnostics["candidates_after_prefilter"] = len(candidates)
    diagnostics["opportunities_analyzed"] = len(opportunities)
    diagnostics["evaluation_errors"] = evaluation_errors
    return opportunities, config, diagnostics


def prefilter_candidates(candidates: list[Any], config: ScannerConfig) -> tuple[list[Any], dict[str, Any]]:
    now = datetime.now(timezone.utc)
    kept: list[Any] = []
    deadline_filtered = 0
    no_price_filtered = 0
    examples: list[dict[str, str]] = []

    for candidate in candidates:
        hours_to_deadline = (candidate.deadline - now).total_seconds() / 3600
        if hours_to_deadline < config.min_hours_to_deadline:
            deadline_filtered += 1
            add_prefilter_example(
                examples,
                candidate,
                f"дедлайн надто близько: {hours_to_deadline:.1f}h < {config.min_hours_to_deadline:.1f}h",
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
    for candidate in candidates:
        try:
            result = scout_candidates(
                [candidate],
                risk_config(config),
                max_futures_margin=config.max_futures_margin,
                use_live_orderbook=config.live_orderbook,
                max_slippage=config.max_slippage,
                max_workers=1,
                polymarket_timeout=config.http_timeout,
            )
        except Exception as exc:
            errors.append(
                {
                    "slug": getattr(candidate, "slug", "unknown"),
                    "question": getattr(candidate, "question", ""),
                    "reason": str(exc),
                }
            )
            continue
        opportunities.extend(result)
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
    if not opportunity.quality.ok:
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


def render_scanner_alert(opportunity: Opportunity) -> str:
    body = render_scout_cards([opportunity], top=1)
    return "<b>🚨 Новий сигнал від 24/7 scanner</b>\n\n" + body


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
        f"no_price={config.min_no_price}-{config.max_no_price}"
    )

    while stop_event is None or not stop_event.is_set():
        started_at = now_iso()
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
            review_summary = review_due_skips(limit=10) if not dry_run else None
            if review_summary is not None and review_summary.reviewed > 0:
                review_text = render_review_summary(review_summary)
                safe_print(review_text)
                if bot is not None and chat_id is not None:
                    bot.send_report(chat_id, TelegramResponse(text=review_text, html=True))
            if not dry_run:
                save_state(state)
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
            "min_no_price": config.min_no_price,
            "max_no_price": config.max_no_price,
            "min_net_upside": config.min_net_upside,
            "min_reward_risk": config.min_reward_risk,
            "live_orderbook": config.live_orderbook,
            "http_timeout": config.http_timeout,
            "max_workers": config.max_workers,
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
