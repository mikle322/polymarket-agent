import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class DeribitVolatility:
    currency: str
    annualized_volatility: float
    source: str
    timestamp: int | None


class DeribitConnector:
    """Public read-only Deribit market connector."""

    def __init__(self, base_url: str = "https://www.deribit.com/api/v2", timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def btc_volatility_index(self, lookback_minutes: int = 30) -> DeribitVolatility:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (lookback_minutes * 60 * 1000)
        payload = self._get_json(
            "/public/get_volatility_index_data",
            {
                "currency": "BTC",
                "start_timestamp": str(start_ms),
                "end_timestamp": str(end_ms),
                "resolution": "60",
            },
        )
        data = ((payload.get("result") or {}).get("data") or [])
        if not data:
            raise RuntimeError(f"Deribit returned no volatility data: {payload}")

        last = data[-1]
        timestamp = int(last[0])
        close_vol = float(last[4])
        if close_vol > 3.0:
            close_vol = close_vol / 100.0
        return DeribitVolatility(currency="BTC", annualized_volatility=close_vol, source="Deribit DVOL", timestamp=timestamp)

    def _get_json(self, path: str, params: dict[str, str]) -> Any:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "polymarket-hedge-bot/0.1"})
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

