import html
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_hedge_bot.formatting import money, ua_reason


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
    radar = diagnostics.get("radar") or {}
    if radar.get("enabled"):
        lines.append(f"• Радар-кандидатів: <b>{radar.get('matched', 0)}</b>")
    lines.extend(render_diagnostics_block(diagnostics))
    lines.extend(render_prefilter_block(diagnostics))
    lines.extend(render_radar_diagnostics_block(diagnostics))
    lines.extend(render_timings_block(diagnostics))

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
            f"• Min time to deadline: <b>{format_optional_hours(status.get('min_hours_to_deadline'))}</b>",
            f"• Max time to deadline: <b>{format_optional_hours(status.get('max_hours_to_deadline'))}</b>",
            f"• NO price range: <b>{format_optional_price(status.get('min_no_price'))} - {format_optional_price(status.get('max_no_price'))}</b>",
            f"• Min net upside: <b>${float(status.get('min_net_upside', 0.0)):.2f}</b>",
            f"• Min reward/risk: <b>{status.get('min_reward_risk', 'n/a')}</b>",
        ]
    )

    error = status.get("error")
    if error:
        lines.extend(["", "⚠️ <b>Остання помилка</b>", esc(error)])

    return "\n".join(lines)


def render_radar_status() -> str:
    status = load_scanner_status()
    if status is None:
        return (
            "🔭 <b>Радар угод</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Поки немає даних. Дочекайся першого scan після запуску бота."
        )

    diagnostics = status.get("diagnostics") or {}
    radar = diagnostics.get("radar") or {}
    if not radar.get("enabled"):
        return (
            "🔭 <b>Радар угод</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Радар зараз вимкнений у параметрах scanner."
        )

    top = radar.get("top") or []
    lines = [
        "🔭 <b>Радар угод</b>",
        "━━━━━━━━━━━━━━━━",
        "М'який режим: це не сигнал на вхід, а список угод для спостереження.",
        "",
        f"• Останній scan: <b>{short_time(status.get('finished_at'))}</b>",
        f"• Після radar pre-filter: <b>{radar.get('candidates_after_prefilter', 'n/a')}</b>",
        f"• Проаналізовано: <b>{radar.get('opportunities_analyzed', 'n/a')}</b>",
        f"• Цікавих для radar: <b>{radar.get('matched', 0)}</b>",
        f"• Відсіяно radar-фільтрами: <b>{radar.get('rejected', 0)}</b>",
        "",
        "🎚 <b>М'які фільтри radar</b>",
        f"• Min score: <b>{status.get('radar_min_score', 'n/a')}</b>",
        f"• Min edge: <b>{format_optional_percent(status.get('radar_min_edge'))}</b>",
        f"• Min NO wins: <b>{format_optional_percent(status.get('radar_min_positive_probability'))}</b>",
        f"• Min time to deadline: <b>{format_optional_hours(status.get('radar_min_hours_to_deadline'))}</b>",
        f"• Max time to deadline: <b>{format_optional_hours(status.get('radar_max_hours_to_deadline'))}</b>",
        f"• NO price range: <b>{format_optional_price(status.get('radar_min_no_price'))} - {format_optional_price(status.get('radar_max_no_price'))}</b>",
        f"• Min net upside: <b>{money(float(status.get('radar_min_net_upside', 0.0)))}</b>",
        f"• Min reward/risk: <b>{status.get('radar_min_reward_risk', 'n/a')}</b>",
    ]

    if not top:
        lines.extend(render_radar_rejection_lines(radar))
        lines.extend(["", f"💡 <b>Чому порожньо:</b> {esc(radar_zero_reason(radar))}"])
        return "\n".join(lines)

    lines.extend(render_radar_rejection_lines(radar))
    lines.extend(["", "📌 <b>Найцікавіші зараз</b>"])
    for index, item in enumerate(top, start=1):
        lines.extend(
            [
                "",
                f"{index}. <b>{esc(item.get('decision', 'n/a'))}</b> | score <b>{float(item.get('score', 0.0)):.1f}</b>",
                f"<code>{esc(item.get('slug', 'n/a'))}</code>",
                f"• Ймовірність позитивного результату: <b>{format_optional_percent(item.get('positive_probability'))}</b>",
                f"• Edge: <b>{format_optional_percent(item.get('edge'))}</b> | NO: <b>{format_optional_price(item.get('no_price'))}</b>",
                f"• Net upside: <b>{money(float(item.get('net_upside', 0.0)))}</b> | R/R: <b>{float(item.get('reward_risk', 0.0)):.2f}</b>",
                f"• Worst-case: <b>{money(float(item.get('worst_case_after_sl', 0.0)))}</b>",
                f"• Futures: <code>{esc(item.get('futures_side', 'n/a'))}</code> <b>{float(item.get('futures_size_btc', 0.0)):.6f} BTC</b> | margin <b>{money(float(item.get('futures_margin', 0.0)))}</b>",
                f"• TP / SL: <b>{money(float(item.get('take_profit', 0.0)))}</b> / <b>{money(float(item.get('stop_loss', 0.0)))}</b>",
                f"• Причина: {esc(ua_reason(str(item.get('reason', ''))))}",
            ]
        )
    return "\n".join(lines)


def render_why_no_signals() -> str:
    status = load_scanner_status()
    if status is None:
        return (
            "🟡 <b>Чому немає сигналів</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Scanner ще не записав жодного статусу. Зачекай перший scan або перевір, чи сервіс запущений."
        )

    diagnostics = status.get("diagnostics") or {}
    prefilter = diagnostics.get("prefilter") or {}
    radar = diagnostics.get("radar") or {}
    rejected_by = radar.get("rejected_by") or {}
    reason = zero_reason(diagnostics)

    lines = [
        "🟡 <b>Чому немає сигналів</b>",
        "━━━━━━━━━━━━━━━━",
        "",
        f"• Останній scan: <b>{short_time(status.get('finished_at'))}</b>",
        f"• Джерело: <b>{esc(status.get('source', 'n/a'))}</b>",
        f"• Кандидатів знайдено: <b>{diagnostics.get('candidates_loaded', 'n/a')}</b>",
        f"• Після pre-filter: <b>{diagnostics.get('candidates_after_prefilter', 'n/a')}</b>",
        f"• Проаналізовано: <b>{diagnostics.get('opportunities_analyzed', status.get('scanned', 'n/a'))}</b>",
        f"• Пройшли alert-фільтри: <b>{status.get('matched', 'n/a')}</b>",
        "",
        f"💡 <b>Головна причина:</b> {esc(reason or 'угоди є, але зараз не проходять фільтри якості.')}",
    ]

    if prefilter:
        lines.extend(
            [
                "",
                "<b>Pre-filter</b>",
                f"• Дедлайн: <b>{prefilter.get('deadline_filtered', 0)}</b>",
                f"  - надто близько: <b>{prefilter.get('deadline_too_close_filtered', 0)}</b>",
                f"  - надто далеко: <b>{prefilter.get('deadline_too_far_filtered', 0)}</b>",
                f"• NO price: <b>{prefilter.get('no_price_filtered', 0)}</b>",
            ]
        )

    if rejected_by:
        lines.extend(["", "<b>Radar відсіяв</b>"])
        for key, value in rejected_by.items():
            if value:
                lines.append(f"• {esc(key)}: <b>{value}</b>")

    lines.extend(render_alert_rejection_diagnostics(diagnostics))

    lines.extend(
        [
            "",
            "<b>Поточні пороги</b>",
            f"• Min score: <b>{status.get('min_score', 'n/a')}</b>",
            f"• Min edge: <b>{format_optional_percent(status.get('min_edge'))}</b>",
            f"• Min net upside: <b>{money(float(status.get('min_net_upside', 0.0)))}</b>",
            f"• Min reward/risk: <b>{status.get('min_reward_risk', 'n/a')}</b>",
            "",
            "Детальніше: /status або /radar",
        ]
    )
    return "\n".join(lines)


def render_alert_rejection_diagnostics(diagnostics: dict[str, Any]) -> list[str]:
    examples = diagnostics.get("alert_rejections") or []
    if not examples:
        return []

    lines = ["", "<b>Точні причини по найближчих угодах</b>"]
    for index, item in enumerate(examples[:5], start=1):
        lines.extend(
            [
                f"{index}. <code>{esc(item.get('slug', 'unknown'))}</code>",
                f"• Етап: <b>{esc(item.get('stage', 'unknown'))}</b>",
                f"• Decision: <b>{esc(item.get('decision', 'n/a'))}</b> | score <b>{float(item.get('score', 0.0)):.1f}</b>",
                f"• Чому пропустили: {esc(item.get('skip_summary', item.get('reason', 'unknown')))}",
                f"• Причина моделі: {esc(item.get('reason', 'unknown'))}",
            ]
        )
        for failure in (item.get("failures") or [])[:6]:
            lines.append(f"  - {esc(failure)}")
    return lines


def render_radar_rejection_lines(radar: dict[str, Any]) -> list[str]:
    rejected_by = radar.get("rejected_by") or {}
    if not rejected_by:
        return []
    return [
        "",
        "🧪 <b>Чому radar відсіює</b>",
        f"• Score: <b>{rejected_by.get('score', 0)}</b>",
        f"• Edge: <b>{rejected_by.get('edge', 0)}</b>",
        f"• Ймовірність: <b>{rejected_by.get('positive_probability', 0)}</b>",
        f"• Net upside: <b>{rejected_by.get('net_upside', 0)}</b>",
        f"• Reward/Risk: <b>{rejected_by.get('reward_risk', 0)}</b>",
        f"• Ліквідність: <b>{rejected_by.get('liquidity', 0)}</b>",
    ]


def radar_zero_reason(radar: dict[str, Any]) -> str:
    prefilter = radar.get("prefilter") or {}
    if int_or_zero(radar.get("candidates_after_prefilter")) == 0:
        deadline_filtered = int_or_zero(prefilter.get("deadline_filtered"))
        no_price_filtered = int_or_zero(prefilter.get("no_price_filtered"))
        if deadline_filtered or no_price_filtered:
            return "кандидати були, але radar pre-filter відсіяв їх по дедлайну або NO price."
        return "scanner не знайшов кандидатів для radar."
    if int_or_zero(radar.get("opportunities_analyzed")) == 0:
        return "кандидати були, але hedge-аналіз їх не пропустив через некоректні або ризикові вхідні дані."
    return "угоди є, але вони не проходять навіть м'які radar-фільтри score, edge, net upside, reward/risk або ліквідності."


def render_radar_diagnostics_block(diagnostics: dict[str, Any]) -> list[str]:
    radar = diagnostics.get("radar") or {}
    if not radar.get("enabled"):
        return []

    lines = [
        "",
        "🔭 <b>Діагностика radar</b>",
        f"• Після radar pre-filter: <b>{radar.get('candidates_after_prefilter', 'n/a')}</b>",
        f"• Проаналізовано radar: <b>{radar.get('opportunities_analyzed', 'n/a')}</b>",
        f"• Пройшли radar: <b>{radar.get('matched', 0)}</b>",
        f"• Відсіяно radar: <b>{radar.get('rejected', 0)}</b>",
    ]
    lines.extend(render_radar_rejection_lines(radar))
    evaluation_errors = radar.get("evaluation_errors") or []
    if evaluation_errors:
        lines.append(f"• Помилки radar hedge-аналізу: <b>{len(evaluation_errors)}</b>")
    return lines


def render_prefilter_block(diagnostics: dict[str, Any]) -> list[str]:
    prefilter = diagnostics.get("prefilter") or {}
    if not prefilter:
        return []

    lines = [
        "",
        "🧹 <b>Pre-filter якості</b>",
        f"• До pre-filter: <b>{diagnostics.get('candidates_loaded', 'n/a')}</b>",
        f"• Після pre-filter: <b>{diagnostics.get('candidates_after_prefilter', 'n/a')}</b>",
        f"• Відсіяно по дедлайну: <b>{prefilter.get('deadline_filtered', 0)}</b>",
        f"  - занадто близько: <b>{prefilter.get('deadline_too_close_filtered', 0)}</b>",
        f"  - занадто далеко: <b>{prefilter.get('deadline_too_far_filtered', 0)}</b>",
        f"• Відсіяно по NO price: <b>{prefilter.get('no_price_filtered', 0)}</b>",
    ]
    examples = prefilter.get("examples") or []
    if examples:
        lines.append("• Приклади:")
        for item in examples[:3]:
            lines.append(f"  - <code>{esc(item.get('slug', 'unknown'))}</code>: {esc(item.get('reason', 'unknown'))}")
    return lines


def render_timings_block(diagnostics: dict[str, Any]) -> list[str]:
    timings = diagnostics.get("timings") or {}
    if not timings:
        return []

    return [
        "",
        "⚡ <b>Швидкість scan</b>",
        f"• Повний цикл: <b>{format_seconds(timings.get('scan_loop_seconds'))}</b>",
        f"• API inputs разом: <b>{format_seconds(timings.get('market_inputs_total_seconds'))}</b>",
        f"• Market data: <b>{format_seconds(timings.get('market_data_seconds'))}</b>",
        f"• IV: <b>{format_seconds(timings.get('iv_seconds'))}</b>",
        f"• Discovery: <b>{format_seconds(timings.get('discovery_seconds', timings.get('candidate_load_seconds')))}</b>",
        f"• Hedge/orderbook аналіз: <b>{format_seconds(timings.get('hedge_analysis_seconds'))}</b>",
        f"• Evaluate total: <b>{format_seconds(timings.get('evaluate_total_seconds'))}</b>",
    ]


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
        f"• Touch markets: <b>{discovery.get('touch_markets', 'n/a')}</b>",
        f"• Settlement markets: <b>{discovery.get('settlement_markets', 'n/a')}</b>",
        f"• Up/down markets: <b>{discovery.get('up_down_markets', 'n/a')}</b>",
        f"• Unsupported type: <b>{discovery.get('unsupported_market_type', 'n/a')}</b>",
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
    prefilter = diagnostics.get("prefilter") or {}
    if int_or_zero(diagnostics.get("candidates_loaded")) > 0 and int_or_zero(
        diagnostics.get("candidates_after_prefilter")
    ) == 0:
        deadline_filtered = int_or_zero(prefilter.get("deadline_filtered"))
        deadline_too_far_filtered = int_or_zero(prefilter.get("deadline_too_far_filtered"))
        no_price_filtered = int_or_zero(prefilter.get("no_price_filtered"))
        if deadline_too_far_filtered:
            return "угоди є, але вони далі нашого горизонту day/week/month і не підходять для швидкої стратегії."
        if deadline_filtered and no_price_filtered:
            return "кандидати були, але pre-filter відсіяв їх через близький дедлайн або нездорову ціну NO."
        if deadline_filtered:
            return "кандидати були, але всі надто близько до дедлайну для нормального хеджу."
        if no_price_filtered:
            return "кандидати були, але ціна NO поза дозволеним діапазоном."

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


def format_optional_price(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return esc(value)


def format_optional_hours(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        hours = float(value)
        if hours >= 24.0:
            return f"{hours / 24.0:.1f}d"
        return f"{hours:.1f}h"
    except (TypeError, ValueError):
        return esc(value)


def format_seconds(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.3f}s"
    except (TypeError, ValueError):
        return esc(value)
