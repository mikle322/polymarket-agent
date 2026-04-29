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
        f"• Проаналізовано кандидатів: <b>{status.get('scanned', 'n/a')}</b>",
        f"• Пройшли alert-фільтри: <b>{status.get('matched', 'n/a')}</b>",
        f"• Надіслано alerts: <b>{status.get('sent', 'n/a')}</b>",
        f"• Записано пропущених: <b>{status.get('skipped_logged', 'n/a')}</b>",
        f"• HTTP timeout: <b>{status.get('http_timeout', 'n/a')}s</b> | workers: <b>{status.get('max_workers', 'n/a')}</b>",
    ]

    diagnostics = status.get("diagnostics") or {}
    lines.extend(render_diagnostics_block(diagnostics))

    lines.extend(
        [
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
    )

    error = status.get("error")
    if error:
        lines.extend(["", "⚠️ <b>Остання помилка</b>", esc(error)])

    return "\n".join(lines)


def render_diagnostics_block(diagnostics: dict[str, Any]) -> list[str]:
    if not diagnostics:
        return [
            "",
            "🧪 <b>Діагностика нуля</b>",
            "• Ще немає деталізації. Вона зʼявиться після наступного scan з оновленим кодом.",
        ]

    discovery = diagnostics.get("discovery") or {}
    if discovery.get("source") == "candidate_file":
        lines = [
            "",
            "🧪 <b>Діагностика нуля</b>",
            "• Джерело: <b>локальний файл кандидатів</b>",
            f"• Кандидатів у файлі: <b>{discovery.get('parsed_candidates', diagnostics.get('candidates_loaded', 'n/a'))}</b>",
            f"• Дійшли до аналізу: <b>{diagnostics.get('opportunities_analyzed', 'n/a')}</b>",
            f"• Помилки hedge-аналізу: <b>{len(diagnostics.get('evaluation_errors') or [])}</b>",
            f"• Пройшли alert-фільтри: <b>{diagnostics.get('matched_alert_filters', 'n/a')}</b>",
            f"• Надіслано після cooldown: <b>{diagnostics.get('sent_after_cooldown', 'n/a')}</b>",
        ]
        reason = zero_reason(diagnostics)
        if reason:
            lines.extend(["", f"💡 <b>Чому зараз 0:</b> {esc(reason)}"])
        return lines

    lines = [
        "",
        "🧪 <b>Діагностика нуля</b>",
        f"• API ринків переглянуто: <b>{discovery.get('api_seen', 'n/a')}</b>",
        f"• API помилок: <b>{discovery.get('api_errors', 'n/a')}</b>",
        f"• Events переглянуто: <b>{discovery.get('event_seen', 'n/a')}</b>",
        f"• Активні з orderbook: <b>{discovery.get('active_orderbook', 'n/a')}</b>",
        f"• BTC-related: <b>{discovery.get('btc_related', 'n/a')}</b>",
        f"• Touch/down keyword: <b>{discovery.get('touch_or_down_keyword', 'n/a')}</b>",
        f"• Event-based без дати: <b>{discovery.get('filtered_non_calendar_deadline', 'n/a')}</b>",
        f"• Strike занадто далеко: <b>{discovery.get('filtered_strike_distance', 'n/a')}</b>",
        f"• Відсіяла ліквідність: <b>{discovery.get('filtered_liquidity', 'n/a')}</b>",
        f"• Не розпарсились поля: <b>{missing_fields_total(discovery)}</b>",
        f"• Дозавантажено по slug: <b>{discovery.get('hydrated_by_slug', 'n/a')}</b>",
        f"• Кандидатів після парсингу: <b>{discovery.get('parsed_candidates', diagnostics.get('candidates_loaded', 'n/a'))}</b>",
        f"• Дійшли до аналізу: <b>{diagnostics.get('opportunities_analyzed', 'n/a')}</b>",
        f"• Помилки hedge-аналізу: <b>{len(diagnostics.get('evaluation_errors') or [])}</b>",
        f"• Пройшли alert-фільтри: <b>{diagnostics.get('matched_alert_filters', 'n/a')}</b>",
    ]

    reason = zero_reason(diagnostics)
    if reason:
        lines.extend(["", f"💡 <b>Чому зараз 0:</b> {esc(reason)}"])
    failed_examples = discovery.get("failed_examples") or []
    if failed_examples:
        lines.extend(["", "🧾 <b>Приклади, які не стали кандидатами</b>"])
        for item in failed_examples[:4]:
            slug = item.get("slug") or "n/a"
            item_reason = item.get("reason") or "unknown"
            lines.append(f"• <code>{esc(slug)}</code>: {esc(item_reason)}")
    error_examples = discovery.get("error_examples") or []
    if error_examples:
        lines.extend(["", "⚠️ <b>API помилки</b>"])
        for error in error_examples[:3]:
            lines.append(f"• {esc(error)}")
    evaluation_errors = diagnostics.get("evaluation_errors") or []
    if evaluation_errors:
        lines.extend(["", "⚠️ <b>Hedge-аналіз не пройшли</b>"])
        for item in evaluation_errors[:4]:
            lines.append(f"• <code>{esc(item.get('slug', 'unknown'))}</code>: {esc(item.get('reason', 'unknown'))}")

    return lines


def missing_fields_total(discovery: dict[str, Any]) -> int:
    keys = ("missing_strike", "missing_direction", "missing_deadline", "missing_no_token", "missing_no_price")
    total = 0
    for key in keys:
        try:
            total += int(discovery.get(key, 0))
        except (TypeError, ValueError):
            continue
    return total


def zero_reason(diagnostics: dict[str, Any]) -> str:
    discovery = diagnostics.get("discovery") or {}
    if "source" in discovery and discovery.get("source") == "candidate_file":
        if int_or_zero(diagnostics.get("opportunities_analyzed")) == 0:
            return "у файлі кандидатів немає ринків для аналізу."
        if int_or_zero(diagnostics.get("matched_alert_filters")) == 0:
            return "кандидати є, але вони не проходять alert-фільтри."
        return ""

    if int_or_zero(discovery.get("api_seen")) == 0:
        if int_or_zero(discovery.get("api_errors")) > 0:
            return "API-запити падають, тому scanner не отримав ринки для аналізу."
        return "Polymarket API не повернув ринки або scan не дійшов до discovery."
    if int_or_zero(discovery.get("active_orderbook")) == 0:
        return "ринків багато, але немає активних ринків з orderbook."
    if int_or_zero(discovery.get("btc_related")) == 0:
        return "у переглянутих сторінках немає BTC-related ринків."
    if int_or_zero(discovery.get("touch_or_down_keyword")) == 0:
        return "BTC-ринки є, але їх назви не схожі на touch/down markets, які бот зараз вміє парсити."
    if int_or_zero(discovery.get("filtered_non_calendar_deadline")) > 0 and int_or_zero(discovery.get("parsed_candidates")) == 0:
        return "BTC-ринки є, але вони мають event-based дедлайн без чіткої календарної дати. Для нашої probability-моделі такі ринки поки небезпечні."
    if int_or_zero(discovery.get("filtered_strike_distance")) > 0 and int_or_zero(discovery.get("parsed_candidates")) == 0:
        return "BTC-ринки є, але strike занадто далеко від поточної ціни BTC, тому hedge/ймовірність стають ненадійними."
    if int_or_zero(discovery.get("filtered_liquidity")) > 0 and int_or_zero(discovery.get("parsed_candidates")) == 0:
        return "кандидати були, але їх відсік min-liquidity."
    if missing_fields_total(discovery) > 0 and int_or_zero(discovery.get("parsed_candidates")) == 0:
        return "BTC-ринки є, але бот не зміг витягнути strike, direction, deadline або NO price."
    if int_or_zero(diagnostics.get("opportunities_analyzed")) == 0:
        if diagnostics.get("evaluation_errors"):
            return "кандидати знайшлись, але hedge-аналіз відхилив їх: strike занадто близько, вже перетнутий або вхідні дані некоректні."
        return "кандидати знайшлись, але не дійшли до повного hedge-аналізу."
    if int_or_zero(diagnostics.get("matched_alert_filters")) == 0:
        return "угоди є, але не проходять наші фільтри якості, edge, ліквідності або ризику."
    if int_or_zero(diagnostics.get("sent_after_cooldown")) == 0:
        return "угоди пройшли фільтри, але не надіслані через cooldown або вже були відправлені раніше."
    return ""


def int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
