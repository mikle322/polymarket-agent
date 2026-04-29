import html
import os
import re
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

from polymarket_hedge_bot.connectors.polymarket_data import PolymarketDataConnector, PolymarketPosition
from polymarket_hedge_bot.formatting import money


WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
ACTIVE_SIZE_THRESHOLD = 0.01
POSITIONS_FETCH_LIMIT = 100


def wallet_from_text(text: str) -> str | None:
    parts = text.strip().split()
    if len(parts) >= 2:
        return parts[1]
    return os.environ.get("POLYMARKET_WALLET_ADDRESS") or os.environ.get("POLYMARKET_PROXY_WALLET")


def render_wallet_positions(wallet: str | None = None, limit: int = 12, timeout: float = 8.0) -> str:
    wallet = wallet or os.environ.get("POLYMARKET_WALLET_ADDRESS") or os.environ.get("POLYMARKET_PROXY_WALLET")
    if not wallet:
        return (
            "💼 <b>Мої позиції Polymarket</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Потрібна тільки публічна адреса wallet/proxy wallet. Приватний ключ не потрібен.\n\n"
            "Додай у <code>.env</code>:\n"
            "<code>POLYMARKET_WALLET_ADDRESS=0x...</code>\n\n"
            "Або відкрий разово так:\n"
            "<code>/positions 0x...</code>"
        )

    if not WALLET_RE.match(wallet):
        return (
            "⚠️ <b>Некоректна адреса</b>\n\n"
            "Адреса має виглядати як <code>0x</code> + 40 hex символів.\n"
            "Приклад: <code>/positions 0x1234...</code>"
        )

    connector = PolymarketDataConnector(timeout=timeout)
    try:
        positions, checked_wallets, proxy_wallet = load_positions_with_proxy_fallback(connector, wallet, limit)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return (
            "⚠️ <b>Не вдалося отримати позиції Polymarket</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"Wallet: <code>{html.escape(short_wallet(wallet))}</code>\n"
            f"Причина: <code>{html.escape(str(exc))}</code>\n\n"
            "Це read-only запит до <code>data-api.polymarket.com/positions</code>. "
            "На VPS має працювати, якщо сервер має доступ до інтернету."
        )
    return render_positions_card(wallet, positions, limit=limit, checked_wallets=checked_wallets, proxy_wallet=proxy_wallet)


def render_position_risk_summary(wallet: str | None = None, limit: int = 100, timeout: float = 8.0) -> str:
    wallet = wallet or os.environ.get("POLYMARKET_WALLET_ADDRESS") or os.environ.get("POLYMARKET_PROXY_WALLET")
    if not wallet:
        return (
            "⚠️ <b>Risk summary</b>\n\n"
            "Спочатку додай <code>POLYMARKET_WALLET_ADDRESS=0x...</code> у <code>.env</code> або виклич <code>/risk 0x...</code>."
        )
    if not WALLET_RE.match(wallet):
        return "⚠️ <b>Некоректна адреса</b>\n\nАдреса має виглядати як <code>0x</code> + 40 hex символів."

    connector = PolymarketDataConnector(timeout=timeout)
    try:
        positions, checked_wallets, proxy_wallet = load_positions_with_proxy_fallback(connector, wallet, limit)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return (
            "⚠️ <b>Не вдалося отримати risk summary</b>\n\n"
            f"Wallet: <code>{html.escape(short_wallet(wallet))}</code>\n"
            f"Причина: <code>{html.escape(str(exc))}</code>"
        )
    return render_risk_card(wallet, positions, checked_wallets=checked_wallets, proxy_wallet=proxy_wallet)


def load_positions_with_proxy_fallback(
    connector: PolymarketDataConnector,
    wallet: str,
    limit: int,
) -> tuple[list[PolymarketPosition], list[str], str | None]:
    checked_wallets: list[str] = []
    proxy_wallet: str | None = None

    try:
        proxy_wallet = connector.get_proxy_wallet(wallet)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        proxy_wallet = None

    wallets = []
    if proxy_wallet:
        wallets.append(proxy_wallet)
    wallets.append(wallet)

    seen: set[str] = set()
    for candidate_wallet in wallets:
        normalized = candidate_wallet.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        checked_wallets.append(candidate_wallet)
        positions = connector.get_positions(
            candidate_wallet,
            limit=max(limit, POSITIONS_FETCH_LIMIT),
            size_threshold=ACTIVE_SIZE_THRESHOLD,
            sort_by="CURRENT",
        )
        active_positions = only_active_positions(positions)
        if active_positions:
            return active_positions, checked_wallets, proxy_wallet

    return [], checked_wallets, proxy_wallet


def render_positions_card(
    wallet: str,
    positions: list[PolymarketPosition],
    limit: int = 12,
    checked_wallets: list[str] | None = None,
    proxy_wallet: str | None = None,
) -> str:
    active = only_active_positions(positions)
    total_current = sum(position.current_value for position in active)
    total_initial = sum(position.initial_value for position in active)
    total_cash_pnl = sum(position.cash_pnl for position in active)
    total_realized = sum(position.realized_pnl for position in active)
    positive = sum(1 for position in active if position.cash_pnl > 0)
    negative = sum(1 for position in active if position.cash_pnl < 0)

    lines = [
        "💼 <b>Мої позиції Polymarket</b>",
        "━━━━━━━━━━━━━━━━",
        f"👛 Wallet: <code>{html.escape(short_wallet(wallet))}</code>",
        f"🔎 Checked: <code>{html.escape(', '.join(short_wallet(item) for item in (checked_wallets or [wallet])))}</code>",
        "🎯 Режим: <b>тільки активні позиції</b>",
        f"📌 Активних позицій: <b>{len(active)}</b>",
        f"💵 Current value: <b>{money(total_current)}</b>",
        f"🧾 Cost basis: <b>{money(total_initial)}</b>",
        f"{pnl_emoji(total_cash_pnl)} Cash PnL: <b>{money(total_cash_pnl)}</b>",
        f"✅ Realized PnL: <b>{money(total_realized)}</b>",
        f"🟢 / 🔴 Плюс-мінус: <b>{positive}</b> / <b>{negative}</b>",
    ]

    if not active:
        lines.extend(
            [
                "",
                proxy_hint(proxy_wallet),
                "Позицій не знайдено. Якщо ти точно маєш угоди, найімовірніше потрібна інша proxy wallet адреса Polymarket.",
                "",
                "Де взяти proxy wallet: Polymarket → profile/settings → адреса під профілем. Можеш також надіслати посилання на свій Polymarket profile.",
            ]
        )
        return "\n".join(lines)

    lines.extend(["", f"📊 <b>Топ {min(limit, len(active))} позицій</b>"])
    for index, position in enumerate(active[:limit], start=1):
        lines.extend(render_position_lines(index, position))

    lines.extend(
        [
            "",
            "ℹ️ Це read-only перегляд через публічний Polymarket Data API. Для цього не потрібен private key.",
        ]
    )
    return "\n".join(lines)


def render_risk_card(
    wallet: str,
    positions: list[PolymarketPosition],
    checked_wallets: list[str] | None = None,
    proxy_wallet: str | None = None,
) -> str:
    active = only_active_positions(positions)
    total_cost = sum(position.initial_value for position in active)
    total_value = sum(position.current_value for position in active)
    total_pnl = sum(position.cash_pnl for position in active)
    max_loss_if_all_zero = total_value
    btc_positions = [position for position in active if is_btc_position(position)]
    btc_value = sum(position.current_value for position in btc_positions)
    btc_cost = sum(position.initial_value for position in btc_positions)
    largest = sorted(active, key=lambda position: position.current_value, reverse=True)[:5]
    near_deadline = sorted(active, key=lambda position: parse_date(position.end_date) or datetime.max.replace(tzinfo=timezone.utc))[:5]

    lines = [
        "🧯 <b>Portfolio risk</b>",
        "━━━━━━━━━━━━━━━━",
        f"👛 Wallet: <code>{html.escape(short_wallet(wallet))}</code>",
        f"🔎 Checked: <code>{html.escape(', '.join(short_wallet(item) for item in (checked_wallets or [wallet])))}</code>",
    ]
    if proxy_wallet:
        lines.append(f"🔁 Proxy: <code>{html.escape(short_wallet(proxy_wallet))}</code>")

    lines.extend(
        [
            "",
            f"• Активних позицій: <b>{len(active)}</b>",
            f"• Current value: <b>{money(total_value)}</b>",
            f"• Cost basis: <b>{money(total_cost)}</b>",
            f"• Cash PnL: <b>{money(total_pnl)}</b>",
            f"• Max loss if all active go to 0: <b>{money(max_loss_if_all_zero)}</b>",
            "",
            "<b>BTC exposure</b>",
            f"• BTC positions: <b>{len(btc_positions)}</b>",
            f"• BTC current value: <b>{money(btc_value)}</b>",
            f"• BTC cost basis: <b>{money(btc_cost)}</b>",
        ]
    )

    if largest:
        lines.extend(["", "<b>Largest active positions</b>"])
        for index, position in enumerate(largest, start=1):
            lines.append(
                f"{index}. <code>{html.escape(position.slug or position.condition_id[:12])}</code> | "
                f"{html.escape(position.outcome or 'Outcome')} | {money(position.current_value)} | PnL {money(position.cash_pnl)}"
            )

    if near_deadline:
        lines.extend(["", "<b>Nearest deadlines</b>"])
        for index, position in enumerate(near_deadline, start=1):
            lines.append(
                f"{index}. <code>{html.escape(short_date(position.end_date))}</code> | "
                f"{html.escape(trim(position.slug or position.title, 48))} | {money(position.current_value)}"
            )

    return "\n".join(lines)


def is_btc_position(position: PolymarketPosition) -> bool:
    text = f"{position.title} {position.slug} {position.event_slug}".lower()
    return "bitcoin" in text or "btc" in text


def proxy_hint(proxy_wallet: str | None) -> str:
    if proxy_wallet:
        return f"Знайдений proxy wallet: <code>{html.escape(short_wallet(proxy_wallet))}</code>"
    return "Proxy wallet автоматично не знайдено для цієї адреси."


def only_active_positions(positions: list[PolymarketPosition]) -> list[PolymarketPosition]:
    now = datetime.now(timezone.utc)
    active: list[PolymarketPosition] = []
    for position in positions:
        if position.size <= ACTIVE_SIZE_THRESHOLD:
            continue
        if position.redeemable or position.mergeable:
            continue
        deadline = parse_date(position.end_date)
        if deadline is not None and deadline <= now:
            continue
        if position.current_value <= 0 or position.cur_price <= 0:
            continue
        active.append(position)
    return active


def render_position_lines(index: int, position: PolymarketPosition) -> list[str]:
    status = position_status(position)
    pnl = position.cash_pnl
    return [
        "",
        f"{index}. {pnl_emoji(pnl)} <b>{html.escape(position.outcome or 'Outcome')}</b> | {status}",
        f"<code>{html.escape(position.slug or position.condition_id[:12])}</code>",
        f"• Ринок: {html.escape(trim(position.title, 90))}",
        f"• Tokens: <b>{position.size:.2f}</b> | Avg: <b>{position.avg_price:.3f}</b> | Now: <b>{position.cur_price:.3f}</b>",
        f"• Value: <b>{money(position.current_value)}</b> | Cost: <b>{money(position.initial_value)}</b>",
        f"• PnL: <b>{money(position.cash_pnl)}</b> | <b>{position.percent_pnl:.1f}%</b>",
        f"• Дедлайн: <code>{html.escape(short_date(position.end_date))}</code>",
    ]


def position_status(position: PolymarketPosition) -> str:
    if position.redeemable:
        return "🎁 <b>redeemable</b>"
    if position.mergeable:
        return "🔀 <b>mergeable</b>"
    end = parse_date(position.end_date)
    if end is not None and end <= datetime.now(timezone.utc):
        return "⏳ <b>після дедлайну</b>"
    return "🟢 <b>активна</b>"


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def short_date(value: str | None) -> str:
    parsed = parse_date(value)
    if parsed is None:
        return "n/a"
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def short_wallet(wallet: str) -> str:
    return f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet


def trim(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def pnl_emoji(value: float) -> str:
    if value > 0:
        return "🟢"
    if value < 0:
        return "🔴"
    return "⚪"
