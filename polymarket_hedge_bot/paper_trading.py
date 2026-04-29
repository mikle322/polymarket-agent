import html
import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from polymarket_hedge_bot.connectors.polymarket import PolymarketConnector, PolymarketMarket
from polymarket_hedge_bot.formatting import money, positive_result_probability
from polymarket_hedge_bot.scout import Opportunity


DATA_DIR = Path("data")
PAPER_TRADES_PATH = DATA_DIR / "paper_trades.jsonl"
_PAPER_LOCK = threading.RLock()


@dataclass(frozen=True)
class PaperTrade:
    paper_id: str
    opened_at: str
    status: str
    slug: str
    question: str
    market_type: str
    deadline: str
    decision: str
    stake: float
    no_price: float
    no_shares: float
    score: float
    positive_probability: float
    edge: float
    net_no_win_after_hedge_sl: float
    net_touch_with_hedge_tp: float
    net_touch_after_hedge_sl_loss: float
    futures_side: str
    futures_size_btc: float
    futures_leverage: float
    worst_case_after_sl: float
    payload: dict[str, Any]
    reviewed_at: str | None = None
    actual_outcome: str | None = None
    hypothetical_pnl: float | None = None
    note: str | None = None


@dataclass(frozen=True)
class PaperReviewSummary:
    checked: int
    reviewed: int
    profitable: int
    pending: int
    errors: list[str]


def record_paper_trades(opportunities: list[Opportunity]) -> int:
    if not opportunities:
        return 0
    with _PAPER_LOCK:
        trades = load_paper_trades()
        existing_open = {trade.slug for trade in trades if trade.status == "OPEN"}
        appended = 0
        for opportunity in opportunities:
            if opportunity.candidate.slug in existing_open:
                continue
            append_paper_trade(opportunity_to_paper_trade(opportunity))
            existing_open.add(opportunity.candidate.slug)
            appended += 1
        return appended


def opportunity_to_paper_trade(opportunity: Opportunity) -> PaperTrade:
    candidate = opportunity.candidate
    return PaperTrade(
        paper_id=uuid4().hex[:10],
        opened_at=now_iso(),
        status="OPEN",
        slug=candidate.slug,
        question=candidate.question,
        market_type=candidate.market_type,
        deadline=candidate.deadline.isoformat(),
        decision=opportunity.decision,
        stake=candidate.stake,
        no_price=opportunity.edge.no_price,
        no_shares=opportunity.pm_shares,
        score=opportunity.score,
        positive_probability=positive_result_probability(opportunity.edge, opportunity.costs),
        edge=opportunity.edge.true_edge,
        net_no_win_after_hedge_sl=opportunity.costs.net_no_win_after_hedge_sl,
        net_touch_with_hedge_tp=opportunity.costs.net_touch_with_hedge_tp,
        net_touch_after_hedge_sl_loss=opportunity.costs.net_touch_after_hedge_sl_loss,
        futures_side=opportunity.hedge.side,
        futures_size_btc=opportunity.hedge.size_btc,
        futures_leverage=opportunity.hedge.leverage,
        worst_case_after_sl=opportunity.worst_case_after_sl,
        payload={
            "strike": candidate.strike,
            "direction": candidate.direction,
            "btc_price": candidate.btc_price,
            "iv": candidate.iv,
            "reason": opportunity.reason,
            "quality": asdict(opportunity.quality),
            "hedge": asdict(opportunity.hedge),
            "costs": asdict(opportunity.costs),
            "liquidity": asdict(opportunity.liquidity),
        },
    )


def append_paper_trade(trade: PaperTrade) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with PAPER_TRADES_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(trade), ensure_ascii=False) + "\n")


def load_paper_trades() -> list[PaperTrade]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PAPER_TRADES_PATH.exists():
        return []
    trades: list[PaperTrade] = []
    for line in PAPER_TRADES_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            trades.append(PaperTrade(**json.loads(line)))
        except (TypeError, json.JSONDecodeError):
            continue
    return trades


def save_paper_trades(trades: list[PaperTrade]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with PAPER_TRADES_PATH.open("w", encoding="utf-8") as handle:
        for trade in trades:
            handle.write(json.dumps(asdict(trade), ensure_ascii=False) + "\n")


def review_due_paper_trades(limit: int = 20) -> PaperReviewSummary:
    with _PAPER_LOCK:
        trades = load_paper_trades()
        if not trades:
            return PaperReviewSummary(checked=0, reviewed=0, profitable=0, pending=0, errors=[])

        connector = PolymarketConnector()
        now = datetime.now(timezone.utc)
        updated: list[PaperTrade] = []
        checked = 0
        reviewed = 0
        profitable = 0
        pending = 0
        errors: list[str] = []

        for trade in trades:
            if reviewed >= limit or trade.status != "OPEN":
                updated.append(trade)
                continue
            if not deadline_passed(trade.deadline, now):
                pending += 1
                updated.append(trade)
                continue

            checked += 1
            try:
                market = connector.get_market_by_slug(trade.slug)
                outcome = infer_binary_outcome(market)
            except Exception as exc:
                errors.append(f"{trade.slug}: {exc}")
                updated.append(trade)
                continue

            if outcome is None:
                pending += 1
                updated.append(trade)
                continue

            pnl = paper_pnl_for_outcome(trade, outcome)
            if pnl > 0:
                profitable += 1
            reviewed += 1
            updated.append(
                PaperTrade(
                    **{
                        **asdict(trade),
                        "status": "CLOSED",
                        "reviewed_at": now.isoformat(),
                        "actual_outcome": outcome,
                        "hypothetical_pnl": pnl,
                        "note": "NO wins scenario" if outcome == "NO" else "touch/YES scenario",
                    }
                )
            )

        save_paper_trades(updated)
        return PaperReviewSummary(checked=checked, reviewed=reviewed, profitable=profitable, pending=pending, errors=errors)


def paper_pnl_for_outcome(trade: PaperTrade, outcome: str) -> float:
    if outcome == "NO":
        return trade.net_no_win_after_hedge_sl
    return trade.net_touch_with_hedge_tp


def render_paper_summary(limit: int = 10) -> str:
    trades = load_paper_trades()
    if not trades:
        return (
            "🧪 <b>Paper trading</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Поки немає paper trades. Вони зʼявляться автоматично, коли scanner знайде сигнал, який проходить alert-фільтри."
        )

    open_trades = [trade for trade in trades if trade.status == "OPEN"]
    closed = [trade for trade in trades if trade.status == "CLOSED"]
    realized = [trade.hypothetical_pnl for trade in closed if trade.hypothetical_pnl is not None]
    total_pnl = sum(realized)
    wins = sum(1 for pnl in realized if pnl > 0)
    losses = sum(1 for pnl in realized if pnl < 0)
    winrate = wins / len(realized) if realized else 0.0

    lines = [
        "🧪 <b>Paper trading</b>",
        "━━━━━━━━━━━━━━━━",
        "",
        f"• Всього paper trades: <b>{len(trades)}</b>",
        f"• Open: <b>{len(open_trades)}</b>",
        f"• Closed: <b>{len(closed)}</b>",
        f"• Hypothetical PnL: <b>{money(total_pnl)}</b>",
        f"• Winrate: <b>{winrate * 100:.1f}%</b>" if realized else "• Winrate: <b>ще немає closed trades</b>",
        f"• Wins/Losses: <b>{wins}</b> / <b>{losses}</b>",
    ]

    if open_trades:
        lines.extend(["", f"<b>Open paper trades</b>"])
        for trade in sorted(open_trades, key=lambda item: item.opened_at, reverse=True)[:limit]:
            lines.extend(render_paper_trade_lines(trade))

    if closed:
        lines.extend(["", f"<b>Latest closed</b>"])
        for trade in sorted(closed, key=lambda item: item.reviewed_at or "", reverse=True)[:limit]:
            lines.extend(render_paper_trade_lines(trade))

    lines.extend(["", "Оновити результати після дедлайнів: /paper_review"])
    return "\n".join(lines)


def render_paper_review_summary(summary: PaperReviewSummary) -> str:
    lines = [
        "🧪 <b>Paper review</b>",
        "━━━━━━━━━━━━━━━━",
        "",
        f"• Перевірено після дедлайну: <b>{summary.checked}</b>",
        f"• Закрито paper trades: <b>{summary.reviewed}</b>",
        f"• Прибуткових: <b>{summary.profitable}</b>",
        f"• Pending/не закриті: <b>{summary.pending}</b>",
    ]
    if summary.reviewed:
        lines.append(f"• Winrate серед оновлених: <b>{summary.profitable / summary.reviewed * 100:.1f}%</b>")
    if summary.errors:
        lines.extend(["", "<b>Помилки</b>"])
        for error in summary.errors[:5]:
            lines.append(f"• {tag(error)}")
    lines.extend(["", "Деталі: /paper"])
    return "\n".join(lines)


def render_paper_trade_lines(trade: PaperTrade) -> list[str]:
    pnl = "" if trade.hypothetical_pnl is None else f" | PnL <b>{money(trade.hypothetical_pnl)}</b>"
    return [
        "",
        f"<code>{tag(trade.paper_id)}</code> | <b>{tag(trade.status)}</b> | {tag(trade.decision)}{pnl}",
        f"<code>{tag(trade.slug)}</code>",
        f"• Stake <b>{money(trade.stake)}</b> | NO <b>{trade.no_price:.3f}</b> | shares <b>{trade.no_shares:.2f}</b>",
        f"• Score <b>{trade.score:.1f}</b> | Edge <b>{trade.edge * 100:.1f}%</b> | Positive <b>{trade.positive_probability * 100:.1f}%</b>",
        f"• Deadline <code>{tag(trade.deadline)}</code>",
    ]


def deadline_passed(value: str, now: datetime) -> bool:
    try:
        deadline = datetime.fromisoformat(value)
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def tag(value: Any) -> str:
    return html.escape(str(value))
