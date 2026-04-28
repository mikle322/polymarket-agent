import argparse
from datetime import datetime, timezone

from polymarket_hedge_bot.config import RiskConfig
from polymarket_hedge_bot.connectors.polymarket import PolymarketConnector
from polymarket_hedge_bot.costs import calculate_costs
from polymarket_hedge_bot.decision import make_decision
from polymarket_hedge_bot.edge import calculate_edge
from polymarket_hedge_bot.formatting import format_analyze_report, format_liquidity_report, format_monitor_report, format_scout_report
from polymarket_hedge_bot.hedge import calculate_futures_hedge
from polymarket_hedge_bot.liquidity import check_basic_liquidity, estimate_buy_from_asks
from polymarket_hedge_bot.monitor import monitor_position
from polymarket_hedge_bot.probability import touch_probability, years_until
from polymarket_hedge_bot.quality import calculate_quality
from polymarket_hedge_bot.scout import load_candidates, scout_candidates
from polymarket_hedge_bot.utils import safe_print


def parse_deadline(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("deadline must be ISO format, for example 2026-05-01") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polymarket-hedge-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Analyze one touch-market setup")
    analyze.add_argument("--slug", required=True)
    analyze.add_argument("--strike", type=float, required=True)
    analyze.add_argument("--direction", choices=["up", "down"], required=True)
    analyze.add_argument("--stake", type=float, required=True)
    analyze.add_argument("--deadline", type=parse_deadline, required=True)
    analyze.add_argument("--btc-price", type=float, required=True)
    analyze.add_argument("--iv", type=float, required=True, help="Annualized volatility, for example 0.55")
    analyze.add_argument("--no-price", type=float, required=True, help="NO entry VWAP, for example 0.57")
    analyze.add_argument("--coverage", type=float)
    analyze.add_argument("--stop-loss", type=float)
    analyze.add_argument("--leverage", type=float)
    analyze.add_argument("--max-futures-margin", type=float)
    analyze.add_argument("--spread", type=float)
    analyze.add_argument("--liquidity", type=float)
    analyze.add_argument("--max-loss", type=float, default=200.0)
    add_cost_args(analyze)

    monitor = subparsers.add_parser("monitor", help="Monitor an open PM position after futures hedge changes")
    monitor.add_argument("--pm-cost", type=float, required=True)
    monitor.add_argument("--pm-current-value", type=float, required=True)
    monitor.add_argument("--pm-shares", type=float, required=True)
    monitor.add_argument("--futures-realized-pnl", type=float, required=True)
    monitor.add_argument("--max-loss", type=float, default=200.0)

    scout = subparsers.add_parser("scout", help="Rank multiple hedge opportunities")
    scout.add_argument("--candidates", required=True, help="Path to a JSON list of candidate markets")
    scout.add_argument("--stake", type=float, help="Default stake when a candidate has no stake")
    scout.add_argument("--max-loss", type=float, default=200.0)
    scout.add_argument("--max-futures-margin", type=float, default=2500.0)
    scout.add_argument("--live-orderbook", action="store_true", help="Use Polymarket CLOB asks for NO VWAP/liquidity")
    scout.add_argument("--max-slippage", type=float, default=0.03)
    scout.add_argument("--top", type=int, default=10)
    add_cost_args(scout)

    pm_liquidity = subparsers.add_parser("pm-liquidity", help="Check live Polymarket CLOB liquidity for buying an outcome")
    pm_liquidity.add_argument("--stake", type=float, required=True)
    pm_liquidity.add_argument("--slug", help="Polymarket market slug")
    pm_liquidity.add_argument("--token-id", help="CLOB token id. If provided, slug lookup is skipped")
    pm_liquidity.add_argument("--outcome", default="No", help="Outcome name to buy when using slug, default: No")
    pm_liquidity.add_argument("--max-vwap", type=float)
    pm_liquidity.add_argument("--max-slippage", type=float, default=0.03)
    return parser


def add_cost_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pm-fee-rate", type=float, default=0.0, help="PM fee rate on stake, e.g. 0.01 for 1 percent")
    parser.add_argument("--futures-fee-rate", type=float, default=0.0005, help="Futures fee rate per side")
    parser.add_argument("--funding-rate", type=float, default=0.0, help="Expected funding rate per period, signed")
    parser.add_argument("--funding-periods", type=float, default=0.0, help="Expected number of funding periods")
    parser.add_argument("--min-net-upside", type=float, default=30.0, help="Minimum acceptable clean net upside")
    parser.add_argument("--min-reward-risk", type=float, default=0.25, help="Minimum acceptable reward/risk")


def build_config(args: argparse.Namespace) -> RiskConfig:
    return RiskConfig(
        max_loss_per_trade=args.max_loss,
        pm_fee_rate=args.pm_fee_rate,
        futures_fee_rate=args.futures_fee_rate,
        funding_rate_per_period=args.funding_rate,
        funding_periods=args.funding_periods,
        min_net_upside=args.min_net_upside,
        min_reward_risk=args.min_reward_risk,
    )


def analyze(args: argparse.Namespace) -> int:
    config = build_config(args)
    liquidity = check_basic_liquidity(args.spread, args.liquidity, args.stake)

    t = years_until(args.deadline)
    fair_touch = touch_probability(args.btc_price, args.strike, args.iv, t, args.direction)
    edge = calculate_edge(fair_touch, args.no_price, config)
    hedge = calculate_futures_hedge(
        pm_invested=args.stake,
        btc_entry=args.btc_price,
        strike=args.strike,
        direction=args.direction,
        config=config,
        coverage=args.coverage,
        stop_loss=args.stop_loss,
        leverage=args.leverage,
        max_futures_margin=args.max_futures_margin,
    )
    costs = calculate_costs(args.stake, args.no_price, hedge, config)
    quality = calculate_quality(costs, config.min_net_upside, config.min_reward_risk)
    decision = make_decision(args.stake, edge, hedge, config, sl_path_cost=costs.total_cost_to_sl, quality=quality)

    final_decision = decision.decision
    final_reason = decision.reason
    if not liquidity.ok:
        final_decision = "SKIP"
        final_reason = liquidity.reason

    safe_print(
        format_analyze_report(
            market=args.slug,
            stake=args.stake,
            decision=final_decision,
            reason=final_reason,
            edge=edge,
            hedge=hedge,
            costs=costs,
            quality=quality,
            worst_case_after_sl=decision.worst_case_after_sl,
            post_sl_action=decision.post_sl_action,
            liquidity=liquidity,
        )
    )
    return 0


def scout(args: argparse.Namespace) -> int:
    config = build_config(args)
    candidates = load_candidates(args.candidates, default_stake=args.stake)
    opportunities = scout_candidates(
        candidates,
        config,
        max_futures_margin=args.max_futures_margin,
        use_live_orderbook=args.live_orderbook,
        max_slippage=args.max_slippage,
    )

    safe_print(format_scout_report(opportunities, args.top))
    return 0


def pm_liquidity(args: argparse.Namespace) -> int:
    if not args.slug and not args.token_id:
        raise SystemExit("Either --slug or --token-id is required")

    connector = PolymarketConnector()
    market = None
    token_id = args.token_id
    if token_id is None:
        market = connector.get_market_by_slug(args.slug)
        token_id = connector.token_id_for_outcome(market, args.outcome)

    book = connector.get_orderbook(token_id)
    result = estimate_buy_from_asks(
        book.asks,
        args.stake,
        max_vwap=args.max_vwap,
        max_slippage=args.max_slippage,
    )

    safe_print(
        format_liquidity_report(
            token_id=token_id,
            result=result,
            market_slug=market.slug if market is not None else None,
            question=market.question if market is not None else None,
            outcome=args.outcome if market is not None else None,
            tick_size=book.tick_size,
            min_order_size=book.min_order_size,
        )
    )
    return 0


def monitor(args: argparse.Namespace) -> int:
    result = monitor_position(
        pm_cost=args.pm_cost,
        pm_current_value=args.pm_current_value,
        pm_shares=args.pm_shares,
        futures_realized_pnl=args.futures_realized_pnl,
        max_loss=args.max_loss,
    )

    safe_print(format_monitor_report(result))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "analyze":
            return analyze(args)
        if args.command == "scout":
            return scout(args)
        if args.command == "monitor":
            return monitor(args)
        if args.command == "pm-liquidity":
            return pm_liquidity(args)
    except ValueError as exc:
        safe_print(f"Помилка: {exc}")
        return 2
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
