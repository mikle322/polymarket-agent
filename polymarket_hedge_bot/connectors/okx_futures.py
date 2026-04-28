import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from polymarket_hedge_bot.connectors._utils import optional_float as _optional_float
from polymarket_hedge_bot.connectors._utils import optional_int as _optional_int
from polymarket_hedge_bot.liquidity import OrderLevel


@dataclass(frozen=True)
class OkxTicker:
    inst_id: str
    last: float
    bid: float
    ask: float
    timestamp: int | None


@dataclass(frozen=True)
class OkxFundingRate:
    inst_id: str
    funding_rate: float
    next_funding_rate: float | None
    funding_time: int | None
    next_funding_time: int | None


@dataclass(frozen=True)
class OkxOrderbook:
    inst_id: str
    bids: list[OrderLevel]
    asks: list[OrderLevel]


class OkxFuturesConnector:
    """Public read-only OKX market connector."""

    def __init__(self, base_url: str = "https://www.okx.com", timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def ticker(self, inst_id: str = "BTC-USDT-SWAP") -> OkxTicker:
        payload = self._get_json("/api/v5/market/ticker", {"instId": inst_id})
        item = self._first_data(payload)
        return OkxTicker(
            inst_id=str(item["instId"]),
            last=float(item["last"]),
            bid=float(item["bidPx"]),
            ask=float(item["askPx"]),
            timestamp=_optional_int(item.get("ts")),
        )

    def funding_rate(self, inst_id: str = "BTC-USDT-SWAP") -> OkxFundingRate:
        payload = self._get_json("/api/v5/public/funding-rate", {"instId": inst_id})
        item = self._first_data(payload)
        return OkxFundingRate(
            inst_id=str(item["instId"]),
            funding_rate=float(item["fundingRate"]),
            next_funding_rate=_optional_float(item.get("nextFundingRate")),
            funding_time=_optional_int(item.get("fundingTime")),
            next_funding_time=_optional_int(item.get("nextFundingTime")),
        )

    def orderbook(self, inst_id: str = "BTC-USDT-SWAP", size: int = 50) -> OkxOrderbook:
        payload = self._get_json("/api/v5/market/books", {"instId": inst_id, "sz": str(size)})
        item = self._first_data(payload)
        return OkxOrderbook(
            inst_id=inst_id,
            bids=[OrderLevel(price=float(row[0]), size=float(row[1])) for row in item.get("bids", [])],
            asks=[OrderLevel(price=float(row[0]), size=float(row[1])) for row in item.get("asks", [])],
        )

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "polymarket-hedge-bot/0.1"})
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _first_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("code") != "0":
            raise RuntimeError(f"OKX API error: {payload}")
        data = payload.get("data") or []
        if not data:
            raise RuntimeError(f"OKX API returned no data: {payload}")
        return data[0]



