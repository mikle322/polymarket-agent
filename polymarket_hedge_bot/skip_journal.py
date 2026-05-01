import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from polymarket_hedge_bot.connectors.polymarket import PolymarketConnector, PolymarketMarket
from polymarket_hedge_bot.formatting import money, pct, ua_reason
from polymarket_hedge_bot.scout import Opportunity


DATA_DIR = Path("data")
SKIPS_PATH = DATA_DIR / "skipped_opportunities.jsonl"
_SKIP_LOCK = threading.RLock()


@dataclass(frozen=True)
class SkipRecord:
    skip_id: str
    created_at: str
    slug: str
    question: str
    deadline: str
    decision: str
    reason: str
    score: float
    no_price: float
    no_win_probability: float
    edge: float
    quality_label: str
    quality_reason: str
    net_upside: float
    reward_risk: float
    worst_case_after_sl: float
    hypothetical_no_win_pnl: float
    hypothetical_touch_pnl: float
    liquidity_ok: bool
    liquidity_reason: str
    reviewed_at: str | None = None
    actual_outcome: str | None = None
    hypothetical_result_pnl: float | None = None
    would_have_been_profitable: bool | None = None
    review_note: str | None = None


@dataclass(frozen=True)
class SkipReviewSummary:
    checked: int
    reviewed: int
    profitable: int
    pending: int
    errors: list[str]


def record_skips(opportunities: list[Opportunity], matched_keys: set[str], cooldown_seconds: int = 6 * 60 * 60) -> int:
    with _SKIP_LOCK:
        records = load_skips()
        now = datetime.now(timezone.utc)
        appended = 0
        for opportunity in opportunities:
            key = opportunity_key(opportunity)
            if key in matched_keys:
                continue
            if was_logged_recently(records, opportunity, now, cooldown_seconds):
                continue
            append_skip(opportunity_to_skip_record(opportunity, now))
            appended += 1
        return appended


def was_logged_recently(
    records: list[SkipRecord],
    opportunity: Opportunity,
    now: datetime,
    cooldown_seconds: int,
) -> bool:
    for record in reversed(records[-500:]):
        if record.slug != opportunity.candidate.slug:
            continue
        if record.reason != opportunity.reason or record.decision != opportunity.decision:
            continue
        try:
            created_at = datetime.fromisoformat(record.created_at)
        except ValueError:
            return False
        return (now - created_at).total_seconds() < cooldown_seconds
    return False


def opportunity_to_skip_record(opportunity: Opportunity, now: datetime) -> SkipRecord:
    candidate = opportunity.candidate
    return SkipRecord(
        skip_id=uuid4().hex[:12],
        created_at=now.isoformat(),
        slug=candidate.slug,
        question=candidate.question,
        deadline=candidate.deadline.isoformat(),
        decision=opportunity.decision,
        reason=opportunity.reason,
        score=opportunity.score,
        no_price=candidate.no_price,
        no_win_probability=opportunity.edge.fair_no,
        edge=opportunity.edge.true_edge,
        quality_label=opportunity.quality.label,
        quality_reason=opportunity.quality.reason,
        net_upside=opportunity.quality.net_upside,
        reward_risk=opportunity.quality.reward_risk,
        worst_case_after_sl=opportunity.worst_case_after_sl,
        hypothetical_no_win_pnl=opportunity.costs.net_no_win_after_hedge_sl,
        hypothetical_touch_pnl=opportunity.costs.net_touch_with_hedge_tp,
        liquidity_ok=opportunity.liquidity.ok,
        liquidity_reason=opportunity.liquidity.reason,
    )


def append_skip(record: SkipRecord) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with SKIPS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def load_skips() -> list[SkipRecord]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SKIPS_PATH.exists():
        return []
    records: list[SkipRecord] = []
    for line in SKIPS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(SkipRecord(**json.loads(line)))
    return records


def save_skips(records: list[SkipRecord]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with SKIPS_PATH.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def latest_unique_by_slug(records: list[SkipRecord]) -> list[SkipRecord]:
    seen: set[str] = set()
    unique: list[SkipRecord] = []
    for record in reversed(records):
        if record.slug in seen:
            continue
        seen.add(record.slug)
        unique.append(record)
    return unique


def render_last_skips(limit: int = 10) -> str:
    records = load_skips()
    if not records:
        return "ОСТАННІ ПРОПУЩЕНІ УГОДИ\n\nПоки немає записів."

    lines = [
        "ОСТАННІ ПРОПУЩЕНІ УГОДИ",
        "",
        f"Показую: {min(limit, len(records))}",
        "",
    ]
    for index, record in enumerate(records[-limit:][::-1], start=1):
        verdict = review_verdict(record)
        lines.extend(
            [
                f"{index}. {record.decision} | score {record.score:.1f} | {record.slug}",
                f"Причина: {ua_reason(record.reason)}",
                f"NO wins: {pct(record.no_win_probability)} | Edge: {pct(record.edge)} | NO: {record.no_price:.3f}",
                f"Якість: {record.quality_label} | Net upside: {money(record.net_upside)} | Reward/Risk: {record.reward_risk:.2f}",
                f"Worst-case: {money(record.worst_case_after_sl)}",
                f"Якби NO wins після SL: {money(record.hypothetical_no_win_pnl)} | Якби touch: {money(record.hypothetical_touch_pnl)}",
                f"Дедлайн: {record.deadline}",
                f"Review: {verdict}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def render_skips_bucket(bucket: str, limit: int = 10) -> str:
    labels = {
        "loss": "ПОВНИЙ МІНУС",
        "flat": "БІЛЯ НУЛЯ / МІНІМАЛЬНИЙ РЕЗУЛЬТАТ",
        "win": "МАКСИМАЛЬНИЙ ПЛЮС",
        "pending": "ЩЕ НЕ ЗАКРИЛИСЬ",
    }
    records = [record for record in load_skips() if skip_bucket(record) == bucket]
    title = labels.get(bucket, bucket.upper())
    if not records:
        return f"ПРОПУЩЕНІ УГОДИ: {title}\n\nПоки немає записів у цій категорії."

    lines = [
        f"ПРОПУЩЕНІ УГОДИ: {title}",
        "",
        f"Знайдено: {len(records)} | Показую: {min(limit, len(records))}",
        "",
    ]
    for index, record in enumerate(records[-limit:][::-1], start=1):
        pnl = record.hypothetical_result_pnl
        pnl_text = "ще немає" if pnl is None else money(pnl)
        lines.extend(
            [
                f"{index}. {record.slug}",
                f"Результат: {record.actual_outcome or 'pending'} | PnL: {pnl_text}",
                f"Причина skip: {ua_reason(record.reason)}",
                f"NO wins: {pct(record.no_win_probability)} | Edge: {pct(record.edge)} | Reward/Risk: {record.reward_risk:.2f}",
                f"Net upside: {money(record.net_upside)} | Worst-case: {money(record.worst_case_after_sl)}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def review_skips(limit: int = 25) -> str:
    return render_review_summary(review_due_skips(limit))


def review_due_skips(limit: int = 25) -> SkipReviewSummary:
    records = load_skips()
    if not records:
        return SkipReviewSummary(checked=0, reviewed=0, profitable=0, pending=0, errors=[])

    connector = PolymarketConnector()
    now = datetime.now(timezone.utc)
    updated: list[SkipRecord] = []
    checked = 0
    reviewed = 0
    profitable = 0
    pending = 0
    errors: list[str] = []

    for record in records:
        if reviewed >= limit or record.reviewed_at is not None:
            updated.append(record)
            continue
        if not deadline_passed(record, now):
            pending += 1
            updated.append(record)
            continue

        checked += 1
        try:
            market = connector.get_market_by_slug(record.slug)
            outcome = infer_binary_outcome(market)
        except Exception as exc:
            errors.append(f"{record.slug}: {exc}")
            updated.append(record)
            continue

        if outcome is None:
            pending += 1
            updated.append(record)
            continue

        pnl = record.hypothetical_no_win_pnl if outcome == "NO" else record.hypothetical_touch_pnl
        is_profitable = pnl > 0
        if is_profitable:
            profitable += 1
        reviewed += 1
        updated.append(
            SkipRecord(
                **{
                    **asdict(record),
                    "reviewed_at": now.isoformat(),
                    "actual_outcome": outcome,
                    "hypothetical_result_pnl": pnl,
                    "would_have_been_profitable": is_profitable,
                    "review_note": "NO wins scenario" if outcome == "NO" else "touch/YES scenario",
                }
            )
        )

    save_skips(updated)
    return SkipReviewSummary(checked=checked, reviewed=reviewed, profitable=profitable, pending=pending, errors=errors)


def render_review_summary(summary: SkipReviewSummary) -> str:
    lines = [
        "REVIEW ПРОПУЩЕНИХ УГОД",
        "",
        f"Перевірено після дедлайну: {summary.checked}",
        f"Оновлено результатів: {summary.reviewed}",
        f"Були б прибуткові: {summary.profitable}",
        f"Ще очікують/не закриті: {summary.pending}",
    ]
    if summary.reviewed:
        lines.append(f"Частка прибуткових серед оновлених: {summary.profitable / summary.reviewed * 100:.1f}%")
    if summary.errors:
        lines.extend(["", "Помилки:", *summary.errors[:5]])
    lines.extend(["", "Деталі дивись через /last_skips"])
    return "\n".join(lines)


def deadline_passed(record: SkipRecord, now: datetime) -> bool:
    try:
        deadline = datetime.fromisoformat(record.deadline)
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline <= now


def infer_binary_outcome(market: PolymarketMarket) -> str | None:
    if not market.closed and not market.archived:
        return None
    yes_price = price_for_outcome(market, "yes")
    no_price = price_for_outcome(market, "no")
    if no_price is not None and no_price >= 0.99:
        return "NO"
    if yes_price is not None and yes_price >= 0.99:
        return "YES"
    if no_price is not None and yes_price is not None:
        return "NO" if no_price > yes_price else "YES"
    return None


def price_for_outcome(market: PolymarketMarket, outcome: str) -> float | None:
    target = outcome.lower()
    for index, name in enumerate(market.outcomes):
        if str(name).lower() == target and index < len(market.outcome_prices):
            return market.outcome_prices[index]
    return None


def opportunity_key(opportunity: Opportunity) -> str:
    return f"{opportunity.candidate.slug}:{opportunity.decision}"


def review_verdict(record: SkipRecord) -> str:
    if record.reviewed_at is None:
        return "ще не перевірено"
    result = "прибуткова" if record.would_have_been_profitable else "збиткова"
    return f"{record.actual_outcome} | {result} | PnL {money(record.hypothetical_result_pnl or 0.0)}"


def skip_bucket(record: SkipRecord) -> str:
    if record.reviewed_at is None or record.hypothetical_result_pnl is None:
        return "pending"
    if record.hypothetical_result_pnl <= -10:
        return "loss"
    if record.hypothetical_result_pnl >= 10:
        return "win"
    return "flat"


def _tag(value: Any) -> str:
    import html as _html

    return _html.escape(str(value))


def _short_dt(value: str | None) -> str:
    if not value:
        return "n/a"
    return _tag(value.replace("T", " ").replace("+00:00", " UTC"))


def _result_emoji(record: SkipRecord) -> str:
    if record.reviewed_at is None:
        return "⏳"
    if record.would_have_been_profitable:
        return "🟢"
    if record.hypothetical_result_pnl is not None and abs(record.hypothetical_result_pnl) < 10:
        return "⚪"
    return "🔴"


def review_verdict(record: SkipRecord) -> str:
    if record.reviewed_at is None:
        return "⏳ ще не перевірено"
    result = "прибуткова" if record.would_have_been_profitable else "збиткова"
    return f"{_result_emoji(record)} {record.actual_outcome} | {result} | PnL {money(record.hypothetical_result_pnl or 0.0)}"


def render_last_skips(limit: int = 10) -> str:
    records = load_skips()
    if not records:
        return (
            "🧩 <b>Пропущені угоди</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Поки немає записів. Коли scanner пропустить кандидата, бот збере його тут для подальшого аналізу."
        )

    unique_records = latest_unique_by_slug(records)
    shown = unique_records[:limit]
    lines = [
        "🧩 <b>Останні пропущені угоди</b>",
        "━━━━━━━━━━━━━━━━",
        f"Показую: <b>{len(shown)}</b> унікальних з <b>{len(records)}</b> записів",
        "",
    ]
    for index, record in enumerate(shown, start=1):
        lines.extend(
            [
                f"{index}. {_result_emoji(record)} <b>{_tag(record.decision)}</b> | score <b>{record.score:.1f}</b>",
                f"<code>{_tag(record.slug)}</code>",
                f"• Причина skip: {_tag(ua_reason(record.reason))}",
                f"• NO wins: <b>{pct(record.no_win_probability)}</b> | Edge: <b>{pct(record.edge)}</b> | NO: <b>{record.no_price:.3f}</b>",
                f"• Якість: <b>{_tag(record.quality_label)}</b> | Net upside: <b>{money(record.net_upside)}</b> | R/R: <b>{record.reward_risk:.2f}</b>",
                f"• Worst-case: <b>{money(record.worst_case_after_sl)}</b>",
                f"• Якби NO wins: <b>{money(record.hypothetical_no_win_pnl)}</b> | якби touch: <b>{money(record.hypothetical_touch_pnl)}</b>",
                f"• Дедлайн: <code>{_short_dt(record.deadline)}</code>",
                f"• Review: {review_verdict(record)}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def render_skips_bucket(bucket: str, limit: int = 10) -> str:
    labels = {
        "loss": ("🔴", "Повний мінус"),
        "flat": ("⚪", "Біля нуля / мінімальний результат"),
        "win": ("🟢", "Максимальний плюс"),
        "pending": ("⏳", "Ще не закрились"),
    }
    icon, title = labels.get(bucket, ("🧩", bucket.upper()))
    raw_records = [record for record in load_skips() if skip_bucket(record) == bucket]
    records = latest_unique_by_slug(raw_records)
    if not records:
        return (
            f"{icon} <b>Пропущені угоди: {title}</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Поки немає записів у цій категорії."
        )

    shown = records[:limit]
    lines = [
        f"{icon} <b>Пропущені угоди: {title}</b>",
        "━━━━━━━━━━━━━━━━",
        f"Знайдено: <b>{len(records)}</b> унікальних | Показую: <b>{len(shown)}</b>",
        "",
    ]
    for index, record in enumerate(shown, start=1):
        pnl = record.hypothetical_result_pnl
        pnl_text = "ще немає" if pnl is None else money(pnl)
        lines.extend(
            [
                f"{index}. <code>{_tag(record.slug)}</code>",
                f"• Результат: <b>{_tag(record.actual_outcome or 'pending')}</b> | PnL: <b>{pnl_text}</b>",
                f"• Причина skip: {_tag(ua_reason(record.reason))}",
                f"• NO wins: <b>{pct(record.no_win_probability)}</b> | Edge: <b>{pct(record.edge)}</b> | R/R: <b>{record.reward_risk:.2f}</b>",
                f"• Net upside: <b>{money(record.net_upside)}</b> | Worst-case: <b>{money(record.worst_case_after_sl)}</b>",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def render_review_summary(summary: SkipReviewSummary) -> str:
    lines = [
        "🔍 <b>Review пропущених угод</b>",
        "━━━━━━━━━━━━━━━━",
        f"• Перевірено після дедлайну: <b>{summary.checked}</b>",
        f"• Оновлено результатів: <b>{summary.reviewed}</b>",
        f"• Були б прибуткові: <b>{summary.profitable}</b>",
        f"• Ще очікують/не закриті: <b>{summary.pending}</b>",
    ]
    if summary.reviewed:
        lines.append(f"• Частка прибуткових: <b>{summary.profitable / summary.reviewed * 100:.1f}%</b>")
    if summary.errors:
        lines.extend(["", "⚠️ <b>Помилки</b>"])
        lines.extend(f"• {_tag(error)}" for error in summary.errors[:5])
    lines.extend(["", "Деталі дивись у розділі <b>Пропущені угоди</b> або через /last_skips."])
    return "\n".join(lines)
