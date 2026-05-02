import json
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from polymarket_hedge_bot.connectors.polymarket_data import PolymarketDataConnector, PolymarketPosition
from polymarket_hedge_bot.formatting import money
from polymarket_hedge_bot.positions import load_positions_with_proxy_fallback, short_wallet, trim
from polymarket_hedge_bot.utils import safe_print


POSITION_MONITOR_STATE_PATH = Path("data") / "position_monitor_state.json"
POSITION_SIZE_EPSILON = 0.01


def run_position_monitor_loop(
    bot: Any,
    chat_id: str,
    interval_seconds: int = 60,
    stop_event: threading.Event | None = None,
) -> None:
    wallet = os.environ.get("POLYMARKET_WALLET_ADDRESS") or os.environ.get("POLYMARKET_PROXY_WALLET")
    if not wallet:
        safe_print("Position monitor disabled: POLYMARKET_WALLET_ADDRESS/POLYMARKET_PROXY_WALLET is missing")
        return

    connector = PolymarketDataConnector(timeout=10.0)
    safe_print(f"Polymarket position monitor started for {short_wallet(wallet)}")

    while stop_event is None or not stop_event.is_set():
        try:
            positions, checked_wallets, proxy_wallet = load_positions_with_proxy_fallback(
                connector,
                wallet,
                limit=100,
            )
            alerts = detect_position_changes(positions)
            if alerts:
                from polymarket_hedge_bot.telegram_bot import TelegramResponse

                for alert in alerts:
                    bot.send_report(chat_id, TelegramResponse(render_position_alert(alert, checked_wallets, proxy_wallet), html=True))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            safe_print(f"Position monitor error: {exc}")

        if stop_event is None:
            time.sleep(interval_seconds)
        else:
            stop_event.wait(interval_seconds)


def render_position_monitor_status() -> str:
    wallet = os.environ.get("POLYMARKET_WALLET_ADDRESS") or os.environ.get("POLYMARKET_PROXY_WALLET")
    state = load_state()
    positions = state.get("positions") or {}
    if not wallet:
        return (
            "🔔 <b>Polymarket fill monitor</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Монітор не зможе працювати без wallet.\n\n"
            "Додай у <code>.env</code>:\n"
            "<code>POLYMARKET_WALLET_ADDRESS=0x...</code>"
        )

    return (
        "🔔 <b>Polymarket fill monitor</b>\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"• Wallet: <code>{escape(short_wallet(wallet))}</code>\n"
        f"• Відомих позицій у snapshot: <b>{len(positions)}</b>\n"
        "• Scanner сигналів: <b>вимкнений</b>\n\n"
        "Коли нова позиція зʼявиться або size збільшиться, бот пришле алерт."
    )


def detect_position_changes(positions: list[PolymarketPosition]) -> list[dict[str, Any]]:
    state = load_state()
    previous = state.get("positions") or {}
    current = {position_key(position): serialize_position(position) for position in positions}

    alerts: list[dict[str, Any]] = []
    if previous:
        for key, position in current.items():
            old = previous.get(key)
            if old is None:
                alerts.append({"kind": "new", "position": position, "delta_size": position["size"]})
                continue
            delta_size = float(position["size"]) - float(old.get("size", 0.0))
            if delta_size > POSITION_SIZE_EPSILON:
                alerts.append({"kind": "increased", "position": position, "delta_size": delta_size})

    save_state({"positions": current, "updated_at": time.time()})
    return alerts


def render_position_alert(alert: dict[str, Any], checked_wallets: list[str], proxy_wallet: str | None) -> str:
    position = alert["position"]
    kind = "нова позиція" if alert["kind"] == "new" else "позиція збільшилась"
    lines = [
        "✅ <b>Polymarket limit order схоже заповнився</b>",
        "━━━━━━━━━━━━━━━━",
        f"• Подія: <b>{kind}</b>",
        f"• Ринок: <code>{escape(position.get('slug') or position.get('condition_id'))}</code>",
        f"• Outcome: <b>{escape(position.get('outcome') or 'n/a')}</b>",
        f"• Додалось shares: <b>{float(alert.get('delta_size', 0.0)):.2f}</b>",
        f"• Поточний size: <b>{float(position.get('size', 0.0)):.2f}</b>",
        f"• Avg price: <b>{float(position.get('avg_price', 0.0)):.3f}</b>",
        f"• Cost basis: <b>{money(float(position.get('initial_value', 0.0)))}</b>",
        f"• Current value: <b>{money(float(position.get('current_value', 0.0)))}</b>",
        f"• PnL: <b>{money(float(position.get('cash_pnl', 0.0)))}</b>",
        f"• Checked wallet: <code>{escape(', '.join(short_wallet(item) for item in checked_wallets))}</code>",
    ]
    if proxy_wallet:
        lines.append(f"• Proxy: <code>{escape(short_wallet(proxy_wallet))}</code>")
    title = trim(str(position.get("title") or ""), 90)
    if title:
        lines.extend(["", escape(title)])
    return "\n".join(lines)


def position_key(position: PolymarketPosition) -> str:
    return f"{position.asset}:{position.condition_id}:{position.outcome_index}"


def serialize_position(position: PolymarketPosition) -> dict[str, Any]:
    data = asdict(position)
    return {
        "asset": data["asset"],
        "condition_id": data["condition_id"],
        "slug": data["slug"],
        "title": data["title"],
        "outcome": data["outcome"],
        "outcome_index": data["outcome_index"],
        "size": data["size"],
        "avg_price": data["avg_price"],
        "initial_value": data["initial_value"],
        "current_value": data["current_value"],
        "cash_pnl": data["cash_pnl"],
        "cur_price": data["cur_price"],
        "end_date": data["end_date"],
    }


def load_state() -> dict[str, Any]:
    if not POSITION_MONITOR_STATE_PATH.exists():
        return {}
    return json.loads(POSITION_MONITOR_STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    POSITION_MONITOR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    POSITION_MONITOR_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def escape(value: Any) -> str:
    import html

    return html.escape(str(value))
