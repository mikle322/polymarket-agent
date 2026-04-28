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
            "СТАТУС БОТА\n\n"
            "Scanner ще не записав жодного статусу.\n"
            "Якщо бот щойно запущений, зачекай 1-2 хвилини."
        )

    ok = bool(status.get("ok"))
    state = "Працює" if ok else "Є помилка"
    lines = [
        "СТАТУС БОТА",
        "",
        f"Стан: {state}",
        f"Останній scan старт: {status.get('started_at', 'n/a')}",
        f"Останній scan кінець: {status.get('finished_at', 'n/a')}",
        f"Джерело: {status.get('source', 'n/a')}",
        f"Інтервал: {status.get('interval_seconds', 'n/a')}s",
        "",
        "РЕЗУЛЬТАТ ОСТАННЬОГО SCAN:",
        f"Перевірено можливостей: {status.get('scanned', 'n/a')}",
        f"Пройшли фільтри: {status.get('matched', 'n/a')}",
        f"Надіслано alerts: {status.get('sent', 'n/a')}",
        f"Записано пропущених: {status.get('skipped_logged', 'n/a')}",
        "",
        "LIVE ДАНІ:",
        f"BTC price input: {format_optional_number(status.get('btc_price'))}",
        f"IV input: {format_optional_percent(status.get('iv'))}",
        f"Funding input: {format_optional_percent(status.get('funding_rate'))}",
        "",
        "ФІЛЬТРИ:",
        f"Decision: {status.get('min_decision', 'n/a')}",
        f"Min score: {status.get('min_score', 'n/a')}",
        f"Min edge: {format_optional_percent(status.get('min_edge'))}",
        f"Min NO wins probability: {format_optional_percent(status.get('min_positive_probability'))}",
        f"Min net upside: ${float(status.get('min_net_upside', 0.0)):.2f}",
        f"Min reward/risk: {status.get('min_reward_risk', 'n/a')}",
    ]

    error = status.get("error")
    if error:
        lines.extend(["", "ОСТАННЯ ПОМИЛКА:", str(error)])

    return "\n".join(lines)


def format_optional_number(value: Any) -> str:
    if value is None:
        return "auto / n/a"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def format_optional_percent(value: Any) -> str:
    if value is None:
        return "auto / n/a"
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return str(value)
