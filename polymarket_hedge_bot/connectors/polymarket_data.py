import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from polymarket_hedge_bot.connectors._utils import optional_float


@dataclass(frozen=True)
class PolymarketPosition:
    proxy_wallet: str
    asset: str
    condition_id: str
    size: float
    avg_price: float
    initial_value: float
    current_value: float
    cash_pnl: float
    percent_pnl: float
    total_bought: float
    realized_pnl: float
    percent_realized_pnl: float
    cur_price: float
    redeemable: bool
    mergeable: bool
    title: str
    slug: str
    event_slug: str
    outcome: str
    outcome_index: int
    opposite_outcome: str
    opposite_asset: str
    end_date: str | None
    negative_risk: bool


class PolymarketDataConnector:
    """Public read-only Polymarket Data API connector."""

    def __init__(
        self,
        data_url: str = "https://data-api.polymarket.com",
        gamma_url: str = "https://gamma-api.polymarket.com",
        timeout: float = 10.0,
    ) -> None:
        self.data_url = data_url.rstrip("/")
        self.gamma_url = gamma_url.rstrip("/")
        self.timeout = timeout

    def get_positions(
        self,
        user: str,
        limit: int = 100,
        offset: int = 0,
        size_threshold: float = 0.0,
        sort_by: str = "CURRENT",
        sort_direction: str = "DESC",
    ) -> list[PolymarketPosition]:
        payload = self._get_json(
            f"{self.data_url}/positions",
            {
                "user": user,
                "limit": str(limit),
                "offset": str(offset),
                "sizeThreshold": str(size_threshold),
                "sortBy": sort_by,
                "sortDirection": sort_direction,
            },
        )
        if not isinstance(payload, list):
            raise ValueError("unexpected Polymarket positions response")
        return [self._parse_position(item) for item in payload if isinstance(item, dict)]

    def get_activity(
        self,
        user: str,
        limit: int = 100,
        offset: int = 0,
        activity_type: str = "TRADE",
    ) -> list[dict[str, Any]]:
        payload = self._get_json(
            f"{self.data_url}/activity",
            {
                "user": user,
                "limit": str(limit),
                "offset": str(offset),
                "type": activity_type,
            },
        )
        if not isinstance(payload, list):
            raise ValueError("unexpected Polymarket activity response")
        return [item for item in payload if isinstance(item, dict)]

    def get_proxy_wallet(self, address: str) -> str | None:
        payload = self._get_json(f"{self.gamma_url}/public-profile", {"address": address})
        if not isinstance(payload, dict):
            return None
        proxy_wallet = payload.get("proxyWallet")
        return str(proxy_wallet) if proxy_wallet else None

    def _get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        if params:
            url = f"{url}?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "polymarket-hedge-bot/0.1"})
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _parse_position(self, payload: dict[str, Any]) -> PolymarketPosition:
        return PolymarketPosition(
            proxy_wallet=str(payload.get("proxyWallet") or ""),
            asset=str(payload.get("asset") or ""),
            condition_id=str(payload.get("conditionId") or ""),
            size=float(payload.get("size") or 0.0),
            avg_price=float(payload.get("avgPrice") or 0.0),
            initial_value=float(payload.get("initialValue") or 0.0),
            current_value=float(payload.get("currentValue") or 0.0),
            cash_pnl=float(payload.get("cashPnl") or 0.0),
            percent_pnl=float(payload.get("percentPnl") or 0.0),
            total_bought=float(payload.get("totalBought") or 0.0),
            realized_pnl=float(payload.get("realizedPnl") or 0.0),
            percent_realized_pnl=float(payload.get("percentRealizedPnl") or 0.0),
            cur_price=float(payload.get("curPrice") or 0.0),
            redeemable=bool(payload.get("redeemable")),
            mergeable=bool(payload.get("mergeable")),
            title=str(payload.get("title") or ""),
            slug=str(payload.get("slug") or ""),
            event_slug=str(payload.get("eventSlug") or ""),
            outcome=str(payload.get("outcome") or ""),
            outcome_index=int(optional_float(payload.get("outcomeIndex")) or 0),
            opposite_outcome=str(payload.get("oppositeOutcome") or ""),
            opposite_asset=str(payload.get("oppositeAsset") or ""),
            end_date=payload.get("endDate"),
            negative_risk=bool(payload.get("negativeRisk")),
        )
