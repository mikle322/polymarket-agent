import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from polymarket_hedge_bot.connectors._utils import optional_float as _optional_float
from polymarket_hedge_bot.connectors._utils import optional_int as _optional_int
from polymarket_hedge_bot.liquidity import OrderLevel


@dataclass(frozen=True)
class BinancePremiumIndex:
    symbol: str
    mark_price: float
    index_price: float
    estimated_settle_price: float | None
    last_funding_rate: float
    next_funding_time: int | None
    interest_rate: float | None
    time: int | None


@dataclass(frozen=True)
class BinanceTickerPrice:
    symbol: str
    price: float
    time: int | None


@dataclass(frozen=True)
class BinanceOrderbook:
    symbol: str
    bids: list[OrderLevel]
    asks: list[OrderLevel]


class BinanceFuturesConnector:
    """Public read-only Binance USD-M futures connector."""

    def __init__(self, base_url: str = "https://fapi.binance.com", timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def ticker_price(self, symbol: str = "BTCUSDT") -> BinanceTickerPrice:
        payload = self._get_json("/fapi/v1/ticker/price", {"symbol": symbol.upper()})
        return BinanceTickerPrice(
            symbol=str(payload["symbol"]),
            price=float(payload["price"]),
            time=_optional_int(payload.get("time")),
        )

    def premium_index(self, symbol: str = "BTCUSDT") -> BinancePremiumIndex:
        payload = self._get_json("/fapi/v1/premiumIndex", {"symbol": symbol.upper()})
        return BinancePremiumIndex(
            symbol=str(payload["symbol"]),
            mark_price=float(payload["markPrice"]),
            index_price=float(payload["indexPrice"]),
            estimated_settle_price=_optional_float(payload.get("estimatedSettlePrice")),
            last_funding_rate=float(payload["lastFundingRate"]),
            next_funding_time=_optional_int(payload.get("nextFundingTime")),
            interest_rate=_optional_float(payload.get("interestRate")),
            time=_optional_int(payload.get("time")),
        )

    def orderbook(self, symbol: str = "BTCUSDT", limit: int = 50) -> BinanceOrderbook:
        payload = self._get_json("/fapi/v1/depth", {"symbol": symbol.upper(), "limit": str(limit)})
        return BinanceOrderbook(
            symbol=symbol.upper(),
            bids=[OrderLevel(price=float(price), size=float(size)) for price, size in payload.get("bids", [])],
            asks=[OrderLevel(price=float(price), size=float(size)) for price, size in payload.get("asks", [])],
        )

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "polymarket-hedge-bot/0.1"})
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))



