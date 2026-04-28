import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from polymarket_hedge_bot.formatting import money


DATA_DIR = Path("data")
SIGNALS_DIR = DATA_DIR / "signals"
JOURNAL_PATH = DATA_DIR / "trade_journal.jsonl"
_JOURNAL_LOCK = threading.RLock()


@dataclass(frozen=True)
class SignalRecord:
    signal_id: str
    created_at: str
    kind: str
    title: str
    decision: str
    positive_probability: float
    payload: dict


@dataclass(frozen=True)
class TradeRecord:
    trade_id: str
    signal_id: str
    entered_at: str
    status: str
    title: str
    decision: str
    positive_probability: float
    payload: dict
    closed_at: str | None = None
    realized_pnl: float | None = None
    note: str | None = None


def create_signal(kind: str, title: str, decision: str, positive_probability: float, payload: dict) -> SignalRecord:
    with _JOURNAL_LOCK:
        ensure_dirs()
        record = SignalRecord(
            signal_id=uuid4().hex[:12],
            created_at=now_iso(),
            kind=kind,
            title=title,
            decision=decision,
            positive_probability=positive_probability,
            payload=payload,
        )
        (SIGNALS_DIR / f"{record.signal_id}.json").write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return record


def load_signal(signal_id: str) -> SignalRecord:
    path = SIGNALS_DIR / f"{signal_id}.json"
    if not path.exists():
        raise ValueError("signal was not found or is too old")
    data = json.loads(path.read_text(encoding="utf-8"))
    return SignalRecord(**data)


def record_entry(signal_id: str) -> TradeRecord:
    with _JOURNAL_LOCK:
        ensure_dirs()
        for existing in load_trades():
            if existing.signal_id == signal_id:
                return existing

        signal = load_signal(signal_id)
        trade = TradeRecord(
            trade_id=uuid4().hex[:10],
            signal_id=signal.signal_id,
            entered_at=now_iso(),
            status="OPEN",
            title=signal.title,
            decision=signal.decision,
            positive_probability=signal.positive_probability,
            payload=signal.payload,
        )
        append_trade(trade)
        return trade


def append_trade(trade: TradeRecord) -> None:
    with _JOURNAL_LOCK:
        ensure_dirs()
        with JOURNAL_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(trade), ensure_ascii=False) + "\n")


def load_trades() -> list[TradeRecord]:
    with _JOURNAL_LOCK:
        ensure_dirs()
        if not JOURNAL_PATH.exists():
            return []
        trades: list[TradeRecord] = []
        for line in JOURNAL_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                trades.append(TradeRecord(**json.loads(line)))
        return trades


def save_trades(trades: list[TradeRecord]) -> None:
    with _JOURNAL_LOCK:
        ensure_dirs()
        with JOURNAL_PATH.open("w", encoding="utf-8") as handle:
            for trade in trades:
                handle.write(json.dumps(asdict(trade), ensure_ascii=False) + "\n")


def close_trade(trade_id: str, realized_pnl: float, note: str | None = None) -> TradeRecord:
    with _JOURNAL_LOCK:
        trades = load_trades()
        updated: list[TradeRecord] = []
        closed_trade: TradeRecord | None = None

        for trade in trades:
            if trade.trade_id != trade_id:
                updated.append(trade)
                continue

            closed_trade = TradeRecord(
                trade_id=trade.trade_id,
                signal_id=trade.signal_id,
                entered_at=trade.entered_at,
                status="CLOSED",
                title=trade.title,
                decision=trade.decision,
                positive_probability=trade.positive_probability,
                payload=trade.payload,
                closed_at=now_iso(),
                realized_pnl=realized_pnl,
                note=note,
            )
            updated.append(closed_trade)

        if closed_trade is None:
            raise ValueError(f"trade {trade_id} was not found")

        save_trades(updated)
        return closed_trade


def journal_summary(limit: int = 10) -> str:
    with _JOURNAL_LOCK:
        trades = load_trades()
    open_trades = [trade for trade in trades if trade.status == "OPEN"]
    closed = [trade for trade in trades if trade.status == "CLOSED"]
    realized = [trade.realized_pnl for trade in closed if trade.realized_pnl is not None]
    total_pnl = sum(realized)
    wins = sum(1 for pnl in realized if pnl > 0)
    losses = sum(1 for pnl in realized if pnl < 0)
    winrate = wins / len(realized) if realized else 0.0

    lines = [
        "ЖУРНАЛ УГОД",
        f"Всього входів: {len(trades)}",
        f"Відкриті: {len(open_trades)}",
        f"Закриті: {len(closed)}",
        f"Realized PnL: {money(total_pnl)}",
        f"Winrate: {winrate * 100:.1f}%" if realized else "Winrate: ще немає закритих угод",
        "",
        f"Останні {min(limit, len(trades))} угод:",
    ]

    for trade in trades[-limit:][::-1]:
        pnl = "" if trade.realized_pnl is None else f" | PnL {money(trade.realized_pnl)}"
        lines.append(
            f"{trade.trade_id} | {trade.status} | {trade.decision} | "
            f"{trade.positive_probability * 100:.1f}% | {trade.title}{pnl}"
        )

    return "\n".join(lines)


def ensure_dirs() -> None:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

