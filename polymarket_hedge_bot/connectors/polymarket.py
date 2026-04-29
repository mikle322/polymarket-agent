import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from polymarket_hedge_bot.connectors._utils import optional_float as _optional_float
from polymarket_hedge_bot.liquidity import OrderLevel


@dataclass(frozen=True)
class PolymarketMarket:
    slug: str
    question: str
    end_date: str | None
    outcomes: list[str]
    token_ids: list[str]
    outcome_prices: list[float]
    liquidity: float | None
    volume_24h: float | None
    active: bool
    closed: bool
    archived: bool
    enable_orderbook: bool


@dataclass(frozen=True)
class PolymarketOrderbook:
    token_id: str
    bids: list[OrderLevel]
    asks: list[OrderLevel]
    tick_size: float | None
    min_order_size: float | None
    last_trade_price: float | None


@dataclass(frozen=True)
class PolymarketEvent:
    slug: str
    title: str
    active: bool
    closed: bool
    markets: list[PolymarketMarket]


class PolymarketConnector:
    """Public read-only Polymarket connector for Gamma and CLOB data."""

    def __init__(
        self,
        gamma_url: str = "https://gamma-api.polymarket.com",
        clob_url: str = "https://clob.polymarket.com",
        timeout: float = 10.0,
    ) -> None:
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.timeout = timeout

    def get_market_by_slug(self, slug: str) -> PolymarketMarket:
        payload = self._get_json(f"{self.gamma_url}/markets/slug/{slug}")
        return self._parse_market(payload)

    def list_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        archived: bool = False,
        order: str = "volume_24hr",
        ascending: bool = False,
    ) -> list[PolymarketMarket]:
        payload = self._get_json(
            f"{self.gamma_url}/markets",
            {
                "limit": str(limit),
                "offset": str(offset),
                "active": str(active).lower(),
                "closed": str(closed).lower(),
                "archived": str(archived).lower(),
                "order": order,
                "ascending": str(ascending).lower(),
            },
        )
        if not isinstance(payload, list):
            raise ValueError("unexpected Polymarket markets response")
        return [self._parse_market(item) for item in payload]

    def list_events(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        archived: bool = False,
        order: str = "volume_24hr",
        ascending: bool = False,
    ) -> list[PolymarketEvent]:
        payload = self._get_json(
            f"{self.gamma_url}/events",
            {
                "limit": str(limit),
                "offset": str(offset),
                "active": str(active).lower(),
                "closed": str(closed).lower(),
                "archived": str(archived).lower(),
                "order": order,
                "ascending": str(ascending).lower(),
            },
        )
        if not isinstance(payload, list):
            raise ValueError("unexpected Polymarket events response")
        return [self._parse_event(item) for item in payload]

    def get_orderbook(self, token_id: str) -> PolymarketOrderbook:
        payload = self._get_json(f"{self.clob_url}/book", {"token_id": token_id})
        return PolymarketOrderbook(
            token_id=str(payload.get("asset_id") or token_id),
            bids=self._parse_levels(payload.get("bids", [])),
            asks=self._parse_levels(payload.get("asks", [])),
            tick_size=_optional_float(payload.get("tick_size")),
            min_order_size=_optional_float(payload.get("min_order_size")),
            last_trade_price=_optional_float(payload.get("last_trade_price")),
        )

    def token_id_for_outcome(self, market: PolymarketMarket, outcome: str) -> str:
        target = outcome.strip().lower()
        for index, name in enumerate(market.outcomes):
            if name.strip().lower() == target:
                try:
                    return market.token_ids[index]
                except IndexError as exc:
                    raise ValueError(f"market has outcome {outcome}, but no matching token id") from exc
        raise ValueError(f"outcome {outcome!r} was not found in market outcomes: {market.outcomes}")

    def _get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        if params:
            url = f"{url}?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "polymarket-hedge-bot/0.1"})
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _parse_market(self, payload: dict[str, Any]) -> PolymarketMarket:
        return PolymarketMarket(
            slug=str(payload.get("slug", "")),
            question=str(payload.get("question", "")),
            end_date=payload.get("endDateIso") or payload.get("endDate"),
            outcomes=_parse_json_list(payload.get("outcomes")),
            token_ids=[str(item) for item in _parse_json_list(payload.get("clobTokenIds"))],
            outcome_prices=[float(item) for item in _parse_json_list(payload.get("outcomePrices"))],
            liquidity=_optional_float(payload.get("liquidityNum") or payload.get("liquidity")),
            volume_24h=_optional_float(payload.get("volume24hr") or payload.get("volume24hrClob")),
            active=bool(payload.get("active")),
            closed=bool(payload.get("closed")),
            archived=bool(payload.get("archived")),
            enable_orderbook=bool(payload.get("enableOrderBook")),
        )

    def _parse_event(self, payload: dict[str, Any]) -> PolymarketEvent:
        markets_payload = payload.get("markets") or []
        markets = [self._parse_market(item) for item in markets_payload if isinstance(item, dict)]
        return PolymarketEvent(
            slug=str(payload.get("slug", "")),
            title=str(payload.get("title") or payload.get("question") or ""),
            active=bool(payload.get("active")),
            closed=bool(payload.get("closed")),
            markets=markets,
        )

    def _parse_levels(self, levels: list[dict[str, Any]]) -> list[OrderLevel]:
        return [
            OrderLevel(price=float(level["price"]), size=float(level["size"]))
            for level in levels
            if "price" in level and "size" in level
        ]


def _parse_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []
