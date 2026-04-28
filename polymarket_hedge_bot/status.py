import html
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATUS_PATH = Path("data") / "scanner_status.json"
_STATUS_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_scanner_status(payload: dict[str, Any]) -> None:
    with _STATUS_LOCK:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_scanner_status() -> dict[str, Any] | None:
    with _STATUS_LOCK:
        if not STATUS_PATH.exists():
            return None
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))


def render_scanner_status() -> str:
    status = load_scanner_status()
    if status is None:
        return (
            "🤖 <b>Статус бота</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "🟡 Scanner ще не записав жодного статусу.\n"
            "Зачекай 1-2 хвилини після запуску VPS-сервісу."
        )

    ok = bool(status.get("ok"))
    state_icon = "🟢" if ok else "🔴"
    state_text = "Працює" if ok else "Є помилка"
    source = esc(status.get("source", "n/a"))

    lines = [
        "🤖 <b>Статус бота</b>",
        "━━━━━━━━━━━━━━━━",
        "",
        f"{state_icon} <b>Стан:</b> {state_text}",
        f"🕒 <b>Останній scan:</b> {short_time(status.get('finished_at'))}",
        f"📡 <b>Джерело:</b> {source}",
        f"⏱ <b>Інтервал:</b> {status.get('interval_seconds', 'n/a')}s",
        "",
        "📊 <b>Результат останнього scan</b>",
        f"• Перевірено ринків: <b>{status.get('scanned', 'n/a')}</b>",
        f"• Пройшли фільтри: <b>{status.get('matched', 'n/a')}</b>",
        f"• Надіслано alerts: <b>{status.get('sent', 'n/a')}</b>",
        f"• Записано пропущених: <b>{status.get('skipped_logged', 'n/a')}</b>",
        "",
        "📈 <b>Live data</b>",
        f"• BTC: <b>{format_optional_number(status.get('btc_price'))}</b>",
        f"• IV: <b>{format_optional_percent(status.get('iv'))}</b>",
        f"• Funding: <b>{format_optional_percent(status.get('funding_rate'))}</b>",
        "",
        "🎚 <b>Фільтри входу</b>",
        f"• Decision: <code>{esc(status.get('min_decision', 'n/a'))}</code>",
        f"• Min score: <b>{status.get('min_score', 'n/a')}</b>",
        f"• Min edge: <b>{format_optional_percent(status.get('min_edge'))}</b>",
        f"• Min NO wins: <b>{format_optional_percent(status.get('min_positive_probability'))}</b>",
        f"• Min net upside: <b>${float(status.get('min_net_upside', 0.0)):.2f}</b>",
        f"• Min reward/risk: <b>{status.get('min_reward_risk', 'n/a')}</b>",
    ]

    error = status.get("error")
    if error:
        lines.extend(["", "⚠️ <b>Остання помилка</b>", esc(error)])

    return "\n".join(lines)


def esc(value: Any) -> str:
    return html.escape(str(value))


def short_time(value: Any) -> str:
    if not value:
        return "n/a"
    return esc(str(value).replace("T", " ").replace("+00:00", " UTC"))


def format_optional_number(value: Any) -> str:
    if value is None:
        return "auto / n/a"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return esc(value)


def format_optional_percent(value: Any) -> str:
    if value is None:
        return "auto / n/a"
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return esc(value)
