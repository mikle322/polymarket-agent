import json
import threading
from dataclasses import asdict, dataclass, replace
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


def create_manual_trade(title: str, note: str | None = None) -> TradeRecord:
    with _JOURNAL_LOCK:
        ensure_dirs()
        trade = TradeRecord(
            trade_id=uuid4().hex[:10],
            signal_id="manual",
            entered_at=now_iso(),
            status="OPEN",
            title=title,
            decision="MANUAL",
            positive_probability=0.0,
            payload={"note": note} if note else {},
        )
        append_trade(trade)
        return trade


def update_pm_leg(
    trade_id: str,
    side: str,
    outcome: str,
    price: float,
    shares: float,
    cost: float | None = None,
    pnl: float | None = None,
) -> TradeRecord:
    return update_trade_payload(
        trade_id,
        {
            "pm_side": side,
            "pm_outcome": outcome,
            "pm_price": price,
            "pm_shares": shares,
            "pm_cost": cost if cost is not None else price * shares,
            "pm_pnl": pnl,
        },
    )


def record_polymarket_position(
    title: str,
    outcome: str,
    price: float,
    shares: float,
    cost: float,
    pnl: float | None = None,
    trade_id: str | None = None,
) -> TradeRecord:
    with _JOURNAL_LOCK:
        target_id = trade_id or latest_open_trade_id()
    if target_id is None:
        trade = create_manual_trade(title)
        target_id = trade.trade_id
    return update_pm_leg(target_id, "BUY", outcome, price, shares, cost, pnl)


def latest_open_trade_id() -> str | None:
    for trade in reversed(load_trades()):
        if trade.status == "OPEN":
            return trade.trade_id
    return None


def update_futures_leg(
    trade_id: str,
    side: str,
    size_btc: float,
    entry_price: float,
    exit_price: float | None = None,
    pnl: float | None = None,
) -> TradeRecord:
    calculated_pnl = pnl
    if calculated_pnl is None and exit_price is not None:
        if side.upper() == "LONG":
            calculated_pnl = (exit_price - entry_price) * size_btc
        elif side.upper() == "SHORT":
            calculated_pnl = (entry_price - exit_price) * size_btc
    return update_trade_payload(
        trade_id,
        {
            "futures_side": side.upper(),
            "futures_size_btc": size_btc,
            "futures_entry_price": entry_price,
            "futures_exit_price": exit_price,
            "futures_pnl": calculated_pnl,
        },
    )


def update_trade_payload(trade_id: str, payload_updates: dict) -> TradeRecord:
    with _JOURNAL_LOCK:
        trades = load_trades()
        updated: list[TradeRecord] = []
        target: TradeRecord | None = None
        for trade in trades:
            if trade.trade_id != trade_id:
                updated.append(trade)
                continue
            payload = dict(trade.payload)
            payload.update({key: value for key, value in payload_updates.items() if value is not None})
            target = replace(trade, payload=payload)
            updated.append(target)
        if target is None:
            raise ValueError(f"trade {trade_id} was not found")
        save_trades(updated)
        return target


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
        for line in JOURNAL_PATH.read_text(encoding="utf-8-sig").splitlines():
            if line.strip():
                trades.append(TradeRecord(**json.loads(line)))
        return trades


def save_trades(trades: list[TradeRecord]) -> None:
    with _JOURNAL_LOCK:
        ensure_dirs()
        with JOURNAL_PATH.open("w", encoding="utf-8") as handle:
            for trade in trades:
                handle.write(json.dumps(asdict(trade), ensure_ascii=False) + "\n")


def close_trade(trade_id: str, realized_pnl: float | None = None, note: str | None = None) -> TradeRecord:
    with _JOURNAL_LOCK:
        trades = load_trades()
        updated: list[TradeRecord] = []
        closed_trade: TradeRecord | None = None

        for trade in trades:
            if trade.trade_id != trade_id:
                updated.append(trade)
                continue

            pnl = realized_pnl if realized_pnl is not None else calculate_total_pnl(trade)
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
                realized_pnl=pnl,
                note=note,
            )
            updated.append(closed_trade)

        if closed_trade is None:
            raise ValueError(f"trade {trade_id} was not found")

        save_trades(updated)
        return closed_trade


def calculate_total_pnl(trade: TradeRecord) -> float:
    payload = trade.payload or {}
    total = 0.0
    found = False
    for key in ("pm_pnl", "futures_pnl"):
        value = payload.get(key)
        if value is None:
            continue
        total += float(value)
        found = True
    return total if found else 0.0


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
        flat = trade.payload.get("net_no_win_flat")
        hedge_be = trade.payload.get("touch_break_even_price")
        scenario = ""
        if flat is not None and hedge_be is not None:
            scenario = f" | flat {money(float(flat))} | hedge BE {money(float(hedge_be))}"
        lines.append(
            f"{trade.trade_id} | {trade.status} | {trade.decision} | "
            f"{trade.positive_probability * 100:.1f}% | {trade.title}{scenario}{pnl}"
        )

    return "\n".join(lines)


def ensure_dirs() -> None:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
