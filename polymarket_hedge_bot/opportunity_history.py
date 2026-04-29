import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_hedge_bot.formatting import money
from polymarket_hedge_bot.formatting import positive_result_probability
from polymarket_hedge_bot.scout import Opportunity


DATA_DIR = Path("data")
OPPORTUNITY_HISTORY_PATH = DATA_DIR / "opportunity_history.jsonl"
_HISTORY_LOCK = threading.RLock()


def record_opportunity_history(
    opportunities: list[Opportunity],
    matched_keys: set[str],
    diagnostics: dict[str, Any],
    limit: int = 100,
) -> int:
    if not opportunities:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    records = [
        opportunity_to_record(opportunity, now, opportunity_key in matched_keys, diagnostics)
        for opportunity, opportunity_key in (
            (item, f"{item.candidate.slug}:{item.decision}") for item in opportunities[:limit]
        )
    ]

    with _HISTORY_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with OPPORTUNITY_HISTORY_PATH.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(records)


def opportunity_to_record(
    opportunity: Opportunity,
    recorded_at: str,
    matched_alert: bool,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    candidate = opportunity.candidate
    return {
        "recorded_at": recorded_at,
        "matched_alert": matched_alert,
        "slug": candidate.slug,
        "question": candidate.question,
        "market_type": candidate.market_type,
        "deadline": candidate.deadline.isoformat(),
        "strike": candidate.strike,
        "direction": candidate.direction,
        "btc_price": candidate.btc_price,
        "iv": candidate.iv,
        "no_price": candidate.no_price,
        "stake": candidate.stake,
        "decision": opportunity.decision,
        "reason": opportunity.reason,
        "score": opportunity.score,
        "positive_probability": positive_result_probability(opportunity.edge, opportunity.costs),
        "edge": asdict(opportunity.edge),
        "quality": asdict(opportunity.quality),
        "hedge": asdict(opportunity.hedge),
        "costs": asdict(opportunity.costs),
        "liquidity": asdict(opportunity.liquidity),
        "pm_shares": opportunity.pm_shares,
        "worst_case_after_sl": opportunity.worst_case_after_sl,
        "risk_ratio": opportunity.risk_ratio,
        "scanner_context": {
            "candidates_loaded": diagnostics.get("candidates_loaded"),
            "candidates_after_prefilter": diagnostics.get("candidates_after_prefilter"),
            "opportunities_analyzed": diagnostics.get("opportunities_analyzed"),
            "matched_alert_filters": diagnostics.get("matched_alert_filters"),
        },
    }


def load_history(limit: int = 500) -> list[dict[str, Any]]:
    with _HISTORY_LOCK:
        if not OPPORTUNITY_HISTORY_PATH.exists():
            return []
        lines = OPPORTUNITY_HISTORY_PATH.read_text(encoding="utf-8").splitlines()

    records: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def render_history_summary(limit: int = 500, top: int = 5) -> str:
    records = load_history(limit=limit)
    if not records:
        return (
            "📚 <b>Scanner history</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Поки немає записів. Вони зʼявляться після наступного scan з оновленим кодом."
        )

    unique_slugs = {str(item.get("slug", "")) for item in records if item.get("slug")}
    matched = [item for item in records if item.get("matched_alert")]
    decisions = count_by(records, "decision")
    market_types = count_by(records, "market_type")
    reasons = top_counts(records, "reason", limit=4)
    best = sorted(records, key=lambda item: float(item.get("score") or 0.0), reverse=True)[:top]
    latest_time = records[-1].get("recorded_at", "n/a")

    lines = [
        "📚 <b>Scanner history</b>",
        "━━━━━━━━━━━━━━━━",
        "",
        f"• Записів у вибірці: <b>{len(records)}</b>",
        f"• Унікальних ринків: <b>{len(unique_slugs)}</b>",
        f"• Alert-matched: <b>{len(matched)}</b>",
        f"• Останній запис: <code>{escape(latest_time)}</code>",
        "",
        "<b>Рішення</b>",
    ]
    for key in ("ENTER", "WATCH", "SKIP"):
        lines.append(f"• {key}: <b>{decisions.get(key, 0)}</b>")

    lines.append("")
    lines.append("<b>Типи ринків</b>")
    for key, value in sorted(market_types.items(), key=lambda item: item[1], reverse=True):
        lines.append(f"• {escape(key)}: <b>{value}</b>")

    if reasons:
        lines.append("")
        lines.append("<b>Найчастіші причини</b>")
        for reason, value in reasons:
            lines.append(f"• {escape(reason)}: <b>{value}</b>")

    lines.append("")
    lines.append("<b>Найближчі до сигналу</b>")
    for index, item in enumerate(best, start=1):
        quality = item.get("quality") or {}
        edge = item.get("edge") or {}
        lines.extend(
            [
                f"{index}. <code>{escape(item.get('slug', 'n/a'))}</code>",
                f"   score <b>{float(item.get('score') or 0.0):.1f}</b> | {escape(item.get('decision', 'n/a'))}",
                f"   edge <b>{float(edge.get('true_edge') or 0.0) * 100:.1f}%</b> | "
                f"net <b>{money(float(quality.get('net_upside') or 0.0))}</b> | "
                f"R/R <b>{float(quality.get('reward_risk') or 0.0):.2f}</b>",
            ]
        )

    return "\n".join(lines)


def count_by(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in records:
        value = str(item.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def top_counts(records: list[dict[str, Any]], key: str, limit: int) -> list[tuple[str, int]]:
    counts = count_by(records, key)
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]


def escape(value: Any) -> str:
    import html

    return html.escape(str(value))
