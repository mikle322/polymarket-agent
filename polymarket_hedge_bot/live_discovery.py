import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from polymarket_hedge_bot.connectors.polymarket import PolymarketConnector, PolymarketMarket
from polymarket_hedge_bot.scout import CandidateMarket
from polymarket_hedge_bot.utils import safe_print


TOUCH_WORDS = (
    "reach",
    "hit",
    "touch",
    "above",
    "higher",
    "high",
    "go to",
    "rise to",
    "trade above",
    "exceed",
    "break",
    "cross",
    "at or above",
    "at least",
    "or more",
)
DOWN_WORDS = (
    "below",
    "under",
    "lower",
    "low",
    "drop",
    "fall",
    "go below",
    "trade below",
    "at or below",
    "less than",
    "or less",
)
BTC_WORDS = ("bitcoin", "btc")


@dataclass
class DiscoveryStats:
    api_seen: int = 0
    api_errors: int = 0
    event_seen: int = 0
    active_orderbook: int = 0
    btc_related: int = 0
    touch_or_down_keyword: int = 0
    filtered_non_calendar_deadline: int = 0
    filtered_strike_distance: int = 0
    filtered_liquidity: int = 0
    missing_strike: int = 0
    missing_direction: int = 0
    missing_deadline: int = 0
    missing_no_token: int = 0
    missing_no_price: int = 0
    parsed_before_dedupe: int = 0
    parsed_candidates: int = 0
    hydrated_by_slug: int = 0
    failed_examples: list[dict[str, str]] = field(default_factory=list)
    error_examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def add_failed_example(self, market: PolymarketMarket, reason: str) -> None:
        if len(self.failed_examples) >= 8:
            return
        self.failed_examples.append(
            {
                "slug": market.slug,
                "question": market.question,
                "reason": reason,
                "end_date": str(market.end_date or ""),
                "outcomes": ", ".join(str(item) for item in market.outcomes),
                "prices": ", ".join(str(item) for item in market.outcome_prices),
                "token_ids": str(len(market.token_ids)),
            }
        )

    def add_error_example(self, error: Exception) -> None:
        self.api_errors += 1
        if len(self.error_examples) < 5:
            self.error_examples.append(str(error))


def discover_polymarket_btc_candidates(
    stake: float,
    btc_price: float,
    iv: float,
    limit: int = 100,
    pages: int = 3,
    min_liquidity: float = 0.0,
    timeout: float = 5.0,
    max_workers: int = 6,
    debug: bool = False,
) -> list[CandidateMarket]:
    candidates, _stats = discover_polymarket_btc_candidates_with_stats(
        stake=stake,
        btc_price=btc_price,
        iv=iv,
        limit=limit,
        pages=pages,
        min_liquidity=min_liquidity,
        timeout=timeout,
        max_workers=max_workers,
        debug=debug,
    )
    return candidates


def discover_polymarket_btc_candidates_with_stats(
    stake: float,
    btc_price: float,
    iv: float,
    limit: int = 100,
    pages: int = 3,
    min_liquidity: float = 0.0,
    timeout: float = 5.0,
    max_workers: int = 6,
    debug: bool = False,
) -> tuple[list[CandidateMarket], DiscoveryStats]:
    candidates: list[CandidateMarket] = []
    stats = DiscoveryStats()

    market_orders = ("volume_24hr", "liquidity", "end_date")

    def fetch_market_page(order: str, page: int) -> list[PolymarketMarket]:
        connector = PolymarketConnector(timeout=timeout)
        return connector.list_markets(limit=limit, offset=page * limit, order=order)

    def fetch_event_page(order: str, page: int) -> list[PolymarketMarket]:
        connector = PolymarketConnector(timeout=timeout)
        events = connector.list_events(limit=limit, offset=page * limit, order=order)
        stats.event_seen += len(events)
        markets: list[PolymarketMarket] = []
        for event in events:
            markets.extend(event.markets)
        return markets

    workers = max(1, min(max_workers, max(1, pages)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = []
        for order in market_orders:
            futures.extend(executor.submit(fetch_market_page, order, page) for page in range(pages))
            futures.extend(executor.submit(fetch_event_page, order, page) for page in range(pages))
        for future in as_completed(futures):
            try:
                markets = future.result()
            except Exception as exc:
                stats.add_error_example(exc)
                continue
            for market in dedupe_markets(markets):
                candidate = market_to_candidate(
                    market,
                    stake,
                    btc_price,
                    iv,
                    min_liquidity,
                    stats=stats,
                    timeout=timeout,
                )
                if candidate is not None:
                    candidates.append(candidate)

    deduped = dedupe_candidates(candidates)
    stats.parsed_candidates = len(deduped)

    if debug:
        safe_print("Discovery debug:")
        for key, value in stats.to_dict().items():
            safe_print(f"  {key}: {value}")

    return deduped, stats


def market_to_candidate(
    market: PolymarketMarket,
    stake: float,
    btc_price: float,
    iv: float,
    min_liquidity: float,
    stats: DiscoveryStats | None = None,
    timeout: float = 5.0,
) -> CandidateMarket | None:
    text = f"{market.question} {market.slug}".lower()
    if stats is not None:
        stats.api_seen += 1

    if not market.active or market.closed or market.archived or not market.enable_orderbook:
        return None
    if stats is not None:
        stats.active_orderbook += 1

    if not any(word in text for word in BTC_WORDS):
        return None
    if stats is not None:
        stats.btc_related += 1

    if not any(word in text for word in TOUCH_WORDS + DOWN_WORDS):
        if stats is not None and any(word in text for word in BTC_WORDS):
            stats.add_failed_example(market, "BTC market, but no touch/down keyword")
        return None
    if stats is not None:
        stats.touch_or_down_keyword += 1

    if not has_calendar_deadline(text):
        if stats is not None:
            stats.filtered_non_calendar_deadline += 1
            stats.add_failed_example(market, "unsupported event-based deadline; no clear calendar date in title")
        return None

    if market.liquidity is not None and market.liquidity < min_liquidity:
        if stats is not None:
            stats.filtered_liquidity += 1
        return None

    enriched_market = market
    strike = parse_strike(text)
    direction = parse_direction(text)
    deadline = parse_deadline(enriched_market.end_date)
    no_token_id = token_id_for_outcome(enriched_market, "No")
    no_price = price_for_outcome(enriched_market, "No")

    if enriched_market.slug and (deadline is None or no_token_id is None or no_price is None):
        try:
            hydrated = PolymarketConnector(timeout=timeout).get_market_by_slug(enriched_market.slug)
            hydrated_text = f"{hydrated.question} {hydrated.slug}".lower()
            enriched_market = hydrated
            text = f"{text} {hydrated_text}"
            strike = strike or parse_strike(text)
            direction = direction or parse_direction(text)
            deadline = deadline or parse_deadline(enriched_market.end_date)
            no_token_id = no_token_id or token_id_for_outcome(enriched_market, "No")
            no_price = no_price if no_price is not None else price_for_outcome(enriched_market, "No")
            if stats is not None:
                stats.hydrated_by_slug += 1
        except Exception:
            pass

    missing = False
    missing_reasons: list[str] = []
    if strike is None:
        missing = True
        missing_reasons.append("missing strike")
        if stats is not None:
            stats.missing_strike += 1
    if direction is None:
        missing = True
        missing_reasons.append("missing direction")
        if stats is not None:
            stats.missing_direction += 1
    if deadline is None:
        missing = True
        missing_reasons.append("missing deadline")
        if stats is not None:
            stats.missing_deadline += 1
    if no_token_id is None:
        missing = True
        missing_reasons.append("missing NO token id")
        if stats is not None:
            stats.missing_no_token += 1
    if no_price is None:
        missing = True
        missing_reasons.append("missing NO price")
        if stats is not None:
            stats.missing_no_price += 1
    if missing:
        if stats is not None:
            stats.add_failed_example(enriched_market, ", ".join(missing_reasons))
        return None

    if not strike_distance_ok(strike, direction, btc_price):
        if stats is not None:
            stats.filtered_strike_distance += 1
            stats.add_failed_example(
                enriched_market,
                f"strike too far for model: strike={strike:.0f}, btc={btc_price:.0f}, direction={direction}",
            )
        return None

    if stats is not None:
        stats.parsed_before_dedupe += 1

    return CandidateMarket(
        slug=enriched_market.slug,
        question=enriched_market.question,
        strike=strike,
        direction=direction,
        deadline=deadline,
        btc_price=btc_price,
        iv=iv,
        no_price=no_price,
        stake=stake,
        liquidity=enriched_market.liquidity,
        no_token_id=no_token_id,
    )


def parse_strike(text: str) -> float | None:
    patterns: list[tuple[str, bool | str]] = [
        (r"\$\s*([0-9]{2,3}(?:,[0-9]{3})+(?:\.\d+)?)", False),
        (r"\b([0-9]{2,3})\s*k\b", True),
        (r"\$\s*([0-9]+(?:\.\d+)?)\s*m\b", "millions"),
        (r"\b([0-9]+(?:\.\d+)?)\s*million\b", "millions"),
        (r"\b([0-9]{5,6})\b", False),
    ]
    for pattern, multiplier in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        raw = match.group(1).replace(",", "")
        value = float(raw)
        if multiplier == "millions":
            value *= 1_000_000
        elif multiplier:
            value *= 1000
        if 10_000 <= value <= 2_000_000:
            return value
    return None


def has_calendar_deadline(text: str) -> bool:
    calendar_patterns = (
        r"\b20[2-4][0-9]\b",
        r"\bjan(?:uary)?\b",
        r"\bfeb(?:ruary)?\b",
        r"\bmar(?:ch)?\b",
        r"\bapr(?:il)?\b",
        r"\bmay\b",
        r"\bjun(?:e)?\b",
        r"\bjul(?:y)?\b",
        r"\baug(?:ust)?\b",
        r"\bsep(?:t(?:ember)?)?\b",
        r"\boct(?:ober)?\b",
        r"\bnov(?:ember)?\b",
        r"\bdec(?:ember)?\b",
        r"\bq[1-4]\b",
        r"\bend of\b",
        r"\bthis (week|month|quarter|year)\b",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in calendar_patterns)


def strike_distance_ok(strike: float, direction: str, btc_price: float) -> bool:
    if btc_price <= 0:
        return False
    ratio = strike / btc_price
    if direction == "up":
        return 1.01 <= ratio <= 3.0
    if direction == "down":
        return 0.33 <= ratio <= 0.99
    return False


def parse_direction(text: str) -> str | None:
    if any(word in text for word in DOWN_WORDS):
        return "down"
    if any(word in text for word in TOUCH_WORDS):
        return "up"
    return None


def parse_deadline(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def token_id_for_outcome(market: PolymarketMarket, outcome: str) -> str | None:
    target = outcome.lower()
    for index, name in enumerate(market.outcomes):
        if str(name).lower() == target:
            if index < len(market.token_ids):
                return market.token_ids[index]
    return None


def price_for_outcome(market: PolymarketMarket, outcome: str) -> float | None:
    target = outcome.lower()
    for index, name in enumerate(market.outcomes):
        if str(name).lower() == target:
            if index < len(market.outcome_prices):
                return market.outcome_prices[index]
    return None


def dedupe_candidates(candidates: list[CandidateMarket]) -> list[CandidateMarket]:
    seen: set[str] = set()
    result: list[CandidateMarket] = []
    for candidate in candidates:
        if candidate.slug in seen:
            continue
        seen.add(candidate.slug)
        result.append(candidate)
    return result


def dedupe_markets(markets: list[PolymarketMarket]) -> list[PolymarketMarket]:
    seen: set[str] = set()
    result: list[PolymarketMarket] = []
    for market in markets:
        key = market.slug or market.question
        if key in seen:
            continue
        seen.add(key)
        result.append(market)
    return result


def candidate_to_json(candidate: CandidateMarket) -> dict:
    data = asdict(candidate)
    data["deadline"] = candidate.deadline.isoformat()
    return data


def save_candidates(path: str | Path, candidates: list[CandidateMarket]) -> None:
    Path(path).write_text(
        json.dumps([candidate_to_json(candidate) for candidate in candidates], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def inspect_btc_markets(limit: int = 100, pages: int = 5) -> list[PolymarketMarket]:
    connector = PolymarketConnector()
    result: list[PolymarketMarket] = []
    for page in range(pages):
        markets = connector.list_markets(limit=limit, offset=page * limit)
        for market in markets:
            text = f"{market.question} {market.slug}".lower()
            if any(word in text for word in BTC_WORDS):
                result.append(market)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polymarket-live-discovery")
    parser.add_argument("--inspect", action="store_true", help="Print BTC markets without converting them to candidates")
    parser.add_argument("--stake", type=float, default=200.0)
    parser.add_argument("--btc-price", type=float)
    parser.add_argument("--iv", type=float)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--min-liquidity", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--output", help="Optional JSON output path")
    parser.add_argument("--debug", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.inspect:
        markets = inspect_btc_markets(limit=args.limit, pages=args.pages)
        safe_print(f"Знайдено BTC markets: {len(markets)}")
        for index, market in enumerate(markets[:50], start=1):
            safe_print(
                f"{index}. {market.slug} | active={market.active} orderbook={market.enable_orderbook} "
                f"liquidity={market.liquidity} | {market.question}"
            )
        return 0

    if args.btc_price is None:
        raise SystemExit("--btc-price is required unless --inspect is used")
    if args.iv is None:
        raise SystemExit("--iv is required unless --inspect is used")

    candidates, stats = discover_polymarket_btc_candidates_with_stats(
        stake=args.stake,
        btc_price=args.btc_price,
        iv=args.iv,
        limit=args.limit,
        pages=args.pages,
        min_liquidity=args.min_liquidity,
        timeout=args.timeout,
        max_workers=args.max_workers,
        debug=args.debug,
    )

    safe_print(f"Знайдено кандидатів: {len(candidates)}")
    if args.debug:
        safe_print(f"Діагностика: {stats.to_dict()}")
    for index, candidate in enumerate(candidates, start=1):
        safe_print(
            f"{index}. {candidate.slug} | {candidate.direction} {candidate.strike:.0f} | "
            f"NO {candidate.no_price:.3f} | liquidity {candidate.liquidity}"
        )

    if args.output:
        save_candidates(args.output, candidates)
        safe_print(f"Збережено: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
