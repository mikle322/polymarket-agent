import argparse
import html
import io
import json
import os
import shlex
import time
from dataclasses import dataclass
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from polymarket_hedge_bot import cli
from polymarket_hedge_bot.config import RiskConfig
from polymarket_hedge_bot.costs import calculate_costs
from polymarket_hedge_bot.edge import calculate_edge
from polymarket_hedge_bot.formatting import money, positive_result_probability
from polymarket_hedge_bot.hedge import calculate_futures_hedge
from polymarket_hedge_bot.journal import close_trade, create_signal, journal_summary, record_entry
from polymarket_hedge_bot.liquidity import check_basic_liquidity
from polymarket_hedge_bot.positions import render_wallet_positions, wallet_from_text
from polymarket_hedge_bot.probability import touch_probability, years_until
from polymarket_hedge_bot.quality import calculate_quality
from polymarket_hedge_bot.scout import load_candidates, scout_candidates
from polymarket_hedge_bot.skip_journal import render_last_skips, render_skips_bucket, review_skips
from polymarket_hedge_bot.status import render_radar_status, render_scanner_status, render_why_no_signals
from polymarket_hedge_bot.telegram_views import render_analyze_card, render_scout_cards
from polymarket_hedge_bot.utils import load_dotenv, safe_print


HELP_TEXT = """Polymarket Hedge Bot

Команди:
/analyze <args>
/scout <args>
/monitor <args>
/pm_liquidity <args>
/status
/last_skips
/review_skips
/journal
/close <trade_id> --pnl <amount> --note "optional note"
/ping
/menu
/help

Приклади:
/scout --candidates examples/candidates.json --max-loss 200 --top 3

/analyze --slug test --strike 80000 --direction up --stake 200 --deadline 2026-05-01 --btc-price 77000 --iv 0.30 --no-price 0.57

/monitor --pm-cost 287.35 --pm-current-value 394.85 --pm-shares 509.5 --futures-realized-pnl -200.58 --max-loss 300

/pm_liquidity --slug market-slug-here --outcome No --stake 200

Після /analyze або /scout можна натиснути кнопку "Зайшов", і бот запише угоду в журнал.
Після виходу з угоди внеси результат: /close trade_id --pnl 42.5

Терміни типу WATCH, SKIP, ENTER, NO, LONG, SHORT, TP, SL, VWAP, funding, edge залишені як трейдинговий сленг.
"""


@dataclass(frozen=True)
class TelegramResponse:
    text: str
    reply_markup: dict[str, Any] | None = None
    html: bool = False


class TelegramBot:
    def __init__(self, token: str, allowed_chat_id: str | None = None, timeout: int = 30) -> None:
        self.token = token
        self.allowed_chat_id = allowed_chat_id
        self.timeout = timeout
        self.base_url = f"https://api.telegram.org/bot{token}"

    def run(self) -> None:
        offset = 0
        print("Telegram bot is running. Press Ctrl+C to stop.")
        while True:
            try:
                updates = self.get_updates(offset)
            except Exception as exc:
                safe_print(f"Failed to get updates: {exc}")
                time.sleep(5)
                continue
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                self.handle_update(update)

    def get_updates(self, offset: int) -> list[dict[str, Any]]:
        response = self.api_call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": self.timeout,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
            timeout=self.timeout + 5,
        )
        return response.get("result", [])

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = str(message.get("text") or "").strip()

        if not chat_id or not text:
            return
        if self.allowed_chat_id and chat_id != self.allowed_chat_id:
            self.send_message(chat_id, "Access denied for this chat.")
            return

        result = handle_text_command(text)
        self.send_report(chat_id, result)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        data = str(callback.get("data") or "")
        callback_id = str(callback.get("id") or "")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))

        if self.allowed_chat_id and chat_id != self.allowed_chat_id:
            self.answer_callback(callback_id, "Access denied")
            return

        if data.startswith("menu:"):
            self.answer_callback(callback_id, "OK")
            self.send_report(chat_id, handle_menu_callback(data))
            return

        if data.startswith("entered:"):
            signal_id = data.split(":", 1)[1]
            try:
                trade = record_entry(signal_id)
            except Exception as exc:
                self.answer_callback(callback_id, f"Помилка: {exc}")
                return
            self.answer_callback(callback_id, "Записано в журнал")
            self.send_report(
                chat_id,
                TelegramResponse(
                    text=(
                        "УГОДУ ЗАПИСАНО\n\n"
                        f"Trade ID: {trade.trade_id}\n"
                        f"Сигнал: {trade.title}\n"
                        f"Рішення на вході: {trade.decision}\n"
                        f"Ймовірність NO wins на момент сигналу: {trade.positive_probability * 100:.1f}%\n\n"
                        "Після закриття угоди ми додамо фіксацію PnL, щоб рахувати реальну статистику."
                    )
                ),
            )
            return

        self.answer_callback(callback_id, "Невідома дія")

    def send_report(self, chat_id: str, response: TelegramResponse | str) -> None:
        text = response.text if isinstance(response, TelegramResponse) else response
        markup = response.reply_markup if isinstance(response, TelegramResponse) else None
        is_html = response.html if isinstance(response, TelegramResponse) else False
        chunks = split_message(text)
        for index, chunk in enumerate(chunks):
            body = chunk if is_html else html.escape(chunk)
            self.send_message(
                chat_id,
                body,
                parse_mode="HTML",
                reply_markup=markup if index == 0 else None,
            )

    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        self.api_call("sendMessage", payload, timeout=10)

    def answer_callback(self, callback_id: str, text: str) -> None:
        if callback_id:
            self.api_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text}, timeout=10)

    def api_call(self, method: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        data = urlencode(payload).encode("utf-8")
        request = Request(f"{self.base_url}/{method}", data=data)
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API error: {body}")
        return body


def handle_text_command(text: str) -> TelegramResponse:
    if text in {"/start", "/menu"}:
        return main_menu_response()
    if text == "/help":
        return TelegramResponse(render_help_card(), reply_markup=main_menu_keyboard(), html=True)
    if text == "/ping":
        return TelegramResponse("🟢 <b>pong</b>\n\nTelegram listener працює.", html=True)
    if text == "/status":
        return TelegramResponse(render_scanner_status(), html=True)
    if text == "/last_skips":
        return TelegramResponse(render_last_skips(), html=True)
    if text == "/review_skips":
        return TelegramResponse(review_skips(), html=True)
    if text == "/journal":
        return TelegramResponse(render_journal_card(), html=True)
    if text.startswith("/close"):
        return TelegramResponse(handle_close_command(text), html=True)

    try:
        argv = telegram_text_to_cli_args(text)
    except ValueError as exc:
        return TelegramResponse(f"⚠️ <b>Помилка команди</b>\n\n{html.escape(str(exc))}", reply_markup=main_menu_keyboard(), html=True)

    if not argv:
        return main_menu_response()

    if argv[0] == "analyze":
        return run_analyze_with_buttons(argv)
    if argv[0] == "scout":
        return run_scout_with_buttons(argv)
    return TelegramResponse(run_cli(argv))


def handle_menu_callback(data: str) -> TelegramResponse:
    action = data.split(":", 1)[1]
    if action == "main":
        return main_menu_response()
    if action == "bot":
        return TelegramResponse("БОТ\n\nОпераційна панель для VPS-бота.", reply_markup=bot_menu_keyboard())
    if action == "bot_status":
        return TelegramResponse(render_scanner_status(), reply_markup=bot_menu_keyboard())
    if action == "bot_ping":
        return TelegramResponse("pong\n\nTelegram listener відповідає.", reply_markup=bot_menu_keyboard())
    if action == "bot_restart":
        return TelegramResponse(
            "РЕСТАРТ БОТА\n\n"
            "Безпечний варіант зараз — через VPS або GitHub auto-deploy.\n\n"
            "VPS команда:\n"
            "systemctl restart polymarket-bot\n\n"
            "GitHub варіант: зроби git push, і Actions сам перезапустить сервіс.",
            reply_markup=bot_menu_keyboard(),
        )
    if action == "bot_stop":
        return TelegramResponse(
            "СТОП БОТА\n\n"
            "Я не ставлю кнопку прямого stop без підтвердження, щоб випадково не вимкнути 24/7 моніторинг.\n\n"
            "VPS команда:\n"
            "systemctl stop polymarket-bot\n\n"
            "Запустити назад:\n"
            "systemctl start polymarket-bot",
            reply_markup=bot_menu_keyboard(),
        )
    if action == "scanner":
        return TelegramResponse(
            "СКАНЕР\n\n"
            "Тут зібрані дії для live scanner: статус, пропущені угоди, review результатів.",
            reply_markup=scanner_menu_keyboard(),
        )
    if action == "scanner_status":
        return TelegramResponse(render_scanner_status(), reply_markup=scanner_menu_keyboard())
    if action == "skips":
        return TelegramResponse(
            "ПРОПУЩЕНІ УГОДИ\n\n"
            "Категорії працюють після review, коли дедлайн минув і Polymarket закрив ринок.",
            reply_markup=skips_menu_keyboard(),
        )
    if action == "skips_last":
        return TelegramResponse(render_last_skips(), reply_markup=skips_menu_keyboard())
    if action == "skips_review":
        return TelegramResponse(review_skips(), reply_markup=skips_menu_keyboard())
    if action == "skips_loss":
        return TelegramResponse(render_skips_bucket("loss"), reply_markup=skips_menu_keyboard())
    if action == "skips_flat":
        return TelegramResponse(render_skips_bucket("flat"), reply_markup=skips_menu_keyboard())
    if action == "skips_win":
        return TelegramResponse(render_skips_bucket("win"), reply_markup=skips_menu_keyboard())
    if action == "skips_pending":
        return TelegramResponse(render_skips_bucket("pending"), reply_markup=skips_menu_keyboard())
    if action == "journal":
        return TelegramResponse(
            journal_summary(),
            reply_markup=journal_menu_keyboard(),
        )
    if action == "journal_help":
        return TelegramResponse(
            "ЖУРНАЛ УГОД\n\n"
            "Коли ти натискаєш кнопку 'Зайшов' під сигналом, бот записує угоду в журнал.\n\n"
            "Після закриття угоди внеси результат командою:\n"
            "/close trade_id --pnl 42.5 --note \"коментар\"",
            reply_markup=journal_menu_keyboard(),
        )
    if action == "help":
        return TelegramResponse(HELP_TEXT, reply_markup=main_menu_keyboard())
    return TelegramResponse("Невідомий пункт меню.", reply_markup=main_menu_keyboard())


def main_menu_response() -> TelegramResponse:
    return TelegramResponse(
        "ГОЛОВНЕ МЕНЮ\n\n"
        "Обери розділ нижче. Команди теж працюють, але кнопками швидше й менше плутанини.",
        reply_markup=main_menu_keyboard(),
    )


def main_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Бот", "callback_data": "menu:bot"}, {"text": "Сканер", "callback_data": "menu:scanner"}],
            [{"text": "Пропущені угоди", "callback_data": "menu:skips"}],
            [{"text": "Журнал", "callback_data": "menu:journal"}, {"text": "Довідка", "callback_data": "menu:help"}],
        ]
    }


def bot_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Статус", "callback_data": "menu:bot_status"}, {"text": "Ping", "callback_data": "menu:bot_ping"}],
            [{"text": "Рестарт", "callback_data": "menu:bot_restart"}, {"text": "Стоп", "callback_data": "menu:bot_stop"}],
            [{"text": "Назад", "callback_data": "menu:main"}],
        ]
    }


def scanner_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Статус scanner", "callback_data": "menu:scanner_status"}],
            [{"text": "🔭 Радар угод", "callback_data": "menu:scanner_radar"}],
            [{"text": "Пропущені угоди", "callback_data": "menu:skips"}],
            [{"text": "Назад", "callback_data": "menu:main"}],
        ]
    }


def skips_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Останні", "callback_data": "menu:skips_last"}, {"text": "Review зараз", "callback_data": "menu:skips_review"}],
            [{"text": "Повний мінус", "callback_data": "menu:skips_loss"}],
            [{"text": "Біля нуля", "callback_data": "menu:skips_flat"}, {"text": "Макс плюс", "callback_data": "menu:skips_win"}],
            [{"text": "Ще не закрились", "callback_data": "menu:skips_pending"}],
            [{"text": "Назад", "callback_data": "menu:main"}],
        ]
    }


def journal_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Оновити журнал", "callback_data": "menu:journal"}],
            [{"text": "Як закрити угоду", "callback_data": "menu:journal_help"}],
            [{"text": "Назад", "callback_data": "menu:main"}],
        ]
    }


def telegram_text_to_cli_args(text: str) -> list[str]:
    command_map = {
        "/analyze": "analyze",
        "/scout": "scout",
        "/monitor": "monitor",
        "/pm_liquidity": "pm-liquidity",
        "/liquidity": "pm-liquidity",
    }
    parts = shlex.split(text)
    if not parts:
        return []

    command = parts[0].split("@", 1)[0]
    if command not in command_map:
        raise ValueError(f"unknown command {parts[0]!r}")

    return [command_map[command], *parts[1:]]


def handle_close_command(text: str) -> str:
    parser = argparse.ArgumentParser(prog="/close")
    parser.add_argument("command")
    parser.add_argument("trade_id")
    parser.add_argument("--pnl", type=float, required=True)
    parser.add_argument("--note")

    try:
        args = parser.parse_args(shlex.split(text))
        trade = close_trade(args.trade_id, args.pnl, args.note)
    except SystemExit:
        return "Формат: /close <trade_id> --pnl <amount> --note \"optional note\""
    except Exception as exc:
        return f"Не вдалося закрити угоду: {exc}"

    return (
        "УГОДУ ЗАКРИТО\n\n"
        f"Trade ID: {trade.trade_id}\n"
        f"Ринок: {trade.title}\n"
        f"Realized PnL: {money(trade.realized_pnl or 0.0)}\n\n"
        "Оновлена статистика:\n"
        f"{journal_summary()}"
    )




def run_analyze_with_buttons(argv: list[str]) -> TelegramResponse:
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    config = cli.build_config(args)
    liquidity = check_basic_liquidity(args.spread, args.liquidity, args.stake)
    fair_touch = touch_probability(args.btc_price, args.strike, args.iv, years_until(args.deadline), args.direction)
    edge = calculate_edge(fair_touch, args.no_price, config)
    hedge = calculate_futures_hedge(
        pm_invested=args.stake,
        btc_entry=args.btc_price,
        strike=args.strike,
        direction=args.direction,
        config=config,
        coverage=args.coverage,
        stop_loss=args.stop_loss,
        leverage=args.leverage,
        max_futures_margin=args.max_futures_margin,
    )
    costs = calculate_costs(args.stake, args.no_price, hedge, config)
    quality = calculate_quality(costs, config.min_net_upside, config.min_reward_risk)
    decision = cli.make_decision(args.stake, edge, hedge, config, sl_path_cost=costs.total_cost_to_sl, quality=quality)

    final_decision = decision.decision
    final_reason = decision.reason
    if not liquidity.ok:
        final_decision = "SKIP"
        final_reason = liquidity.reason

    text = render_analyze_card(
        market=args.slug,
        stake=args.stake,
        decision=final_decision,
        reason=final_reason,
        edge=edge,
        hedge=hedge,
        costs=costs,
        quality=quality,
        worst_case_after_sl=decision.worst_case_after_sl,
        liquidity=liquidity,
    )
    signal = create_signal(
        kind="analyze",
        title=args.slug,
        decision=final_decision,
        positive_probability=positive_result_probability(edge, costs),
        payload={
            "command": argv,
            "stake": args.stake,
            "decision": final_decision,
            "edge": edge.true_edge,
            "positive_probability": positive_result_probability(edge, costs),
            "futures_side": hedge.side,
            "futures_size_btc": hedge.size_btc,
            "futures_leverage": hedge.leverage,
            "worst_case_after_sl": decision.worst_case_after_sl,
        },
    )
    return TelegramResponse(text=text, reply_markup=entered_keyboard(signal.signal_id), html=True)


def run_scout_with_buttons(argv: list[str]) -> TelegramResponse:
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    config = cli.build_config(args)
    candidates = load_candidates(args.candidates, default_stake=args.stake)
    opportunities = scout_candidates(
        candidates,
        config,
        max_futures_margin=args.max_futures_margin,
        use_live_orderbook=args.live_orderbook,
        max_slippage=args.max_slippage,
    )
    shown = opportunities[: args.top]
    text = render_scout_cards(opportunities, args.top)
    buttons = []
    for index, opportunity in enumerate(shown, start=1):
        signal = create_signal(
            kind="scout",
            title=opportunity.candidate.slug,
            decision=opportunity.decision,
            positive_probability=positive_result_probability(opportunity.edge, opportunity.costs),
            payload={
                "command": argv,
                "rank": index,
                "slug": opportunity.candidate.slug,
                "stake": opportunity.candidate.stake,
                "decision": opportunity.decision,
                "edge": opportunity.edge.true_edge,
                "positive_probability": positive_result_probability(opportunity.edge, opportunity.costs),
                "futures_side": opportunity.hedge.side,
                "futures_size_btc": opportunity.hedge.size_btc,
                "futures_leverage": opportunity.hedge.leverage,
                "worst_case_after_sl": opportunity.worst_case_after_sl,
            },
        )
        buttons.append([{"text": f"Зайшов #{index}", "callback_data": f"entered:{signal.signal_id}"}])
    return TelegramResponse(text=text, reply_markup={"inline_keyboard": buttons} if buttons else None, html=True)


def entered_keyboard(signal_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "Зайшов", "callback_data": f"entered:{signal_id}"}]]}


def run_cli(argv: list[str]) -> str:
    stdout = io.StringIO()
    stderr = io.StringIO()
    parser = cli.build_parser()

    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            args = parser.parse_args(argv)
            if args.command == "analyze":
                cli.analyze(args)
            elif args.command == "scout":
                cli.scout(args)
            elif args.command == "monitor":
                cli.monitor(args)
            elif args.command == "pm-liquidity":
                cli.pm_liquidity(args)
            else:
                parser.error("unknown command")
    except SystemExit as exc:
        output = stderr.getvalue() or stdout.getvalue()
        return output.strip() or f"Command exited with code {exc.code}"
    except Exception as exc:
        output = stdout.getvalue().strip()
        prefix = f"Error: {exc}"
        return f"{prefix}\n\nPartial output:\n{output}" if output else prefix

    output = stdout.getvalue().strip()
    return output or "Done."


def split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def render_help_card() -> str:
    return (
        "✨ <b>Polymarket Hedge Bot</b>\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "Я шукаю hedge-угоди: <b>Polymarket NO</b> + <b>futures hedge</b>, рахую risk, edge, funding, fee, ліквідність і даю зрозумілий висновок.\n\n"
        "🧭 <b>Основне</b>\n"
        "• <code>/menu</code> — кнопкова панель\n"
        "• <code>/status</code> — стан scanner\n"
        "• <code>/radar</code> — м'який список угод для спостереження\n"
        "• <code>/journal</code> — журнал угод\n"
        "• <code>/positions</code> — мої позиції Polymarket по wallet address\n"
        "• <code>/last_skips</code> — пропущені угоди\n"
        "• <code>/review_skips</code> — перевірити пропущені після дедлайну\n\n"
        "⚙️ <b>Ручні команди</b>\n"
        "• <code>/scout ...</code>\n"
        "• <code>/analyze ...</code>\n"
        "• <code>/monitor ...</code>\n"
        "• <code>/pm_liquidity ...</code>\n\n"
        "Трейдинговий сленг <b>WATCH / SKIP / ENTER / NO / LONG / SHORT / TP / SL / edge / funding</b> залишаю як є."
    )


def render_journal_card(limit: int = 10) -> str:
    from polymarket_hedge_bot.journal import load_trades

    trades = load_trades()
    open_trades = [trade for trade in trades if trade.status == "OPEN"]
    closed = [trade for trade in trades if trade.status == "CLOSED"]
    realized = [trade.realized_pnl for trade in closed if trade.realized_pnl is not None]
    total_pnl = sum(realized)
    wins = sum(1 for pnl in realized if pnl > 0)
    losses = sum(1 for pnl in realized if pnl < 0)
    winrate = wins / len(realized) if realized else None

    lines = [
        "📒 <b>Журнал угод</b>",
        "━━━━━━━━━━━━━━━━",
        f"• Всього входів: <b>{len(trades)}</b>",
        f"• Відкриті: <b>{len(open_trades)}</b>",
        f"• Закриті: <b>{len(closed)}</b>",
        f"• Realized PnL: <b>{money(total_pnl)}</b>",
        f"• Winrate: <b>{winrate * 100:.1f}%</b>" if winrate is not None else "• Winrate: <b>ще немає закритих угод</b>",
        f"• Wins / Losses: <b>{wins}</b> / <b>{losses}</b>",
        "",
        f"🧾 <b>Останні {min(limit, len(trades))} угод</b>",
    ]

    if not trades:
        lines.append("Поки немає записів. Натисни <b>✅ Зайшов</b> під сигналом, коли реально входиш в угоду.")
        return "\n".join(lines)

    for trade in trades[-limit:][::-1]:
        icon = "🟢" if trade.status == "OPEN" else "✅"
        pnl = "" if trade.realized_pnl is None else f" | PnL <b>{money(trade.realized_pnl)}</b>"
        lines.extend(
            [
                "",
                f"{icon} <code>{html.escape(trade.trade_id)}</code> | <b>{html.escape(trade.status)}</b>",
                f"• {html.escape(trade.decision)} | шанс: <b>{trade.positive_probability * 100:.1f}%</b>{pnl}",
                f"• {html.escape(trade.title)}",
            ]
        )
    return "\n".join(lines)


def main_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🤖 Бот", "callback_data": "menu:bot"}, {"text": "🔎 Сканер", "callback_data": "menu:scanner"}],
            [{"text": "🔭 Радар угод", "callback_data": "menu:scanner_radar"}],
            [{"text": "💼 Мої позиції", "callback_data": "menu:positions"}],
            [{"text": "🧩 Пропущені угоди", "callback_data": "menu:skips"}],
            [{"text": "📒 Журнал", "callback_data": "menu:journal"}, {"text": "✨ Довідка", "callback_data": "menu:help"}],
        ]
    }


def bot_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "📡 Статус", "callback_data": "menu:bot_status"}, {"text": "🟢 Ping", "callback_data": "menu:bot_ping"}],
            [{"text": "🔄 Рестарт", "callback_data": "menu:bot_restart"}, {"text": "⏸ Стоп", "callback_data": "menu:bot_stop"}],
            [{"text": "⬅️ Назад", "callback_data": "menu:main"}],
        ]
    }


def scanner_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "📡 Статус scanner", "callback_data": "menu:scanner_status"}],
            [{"text": "🟡 Чому немає сигналів", "callback_data": "menu:scanner_why"}],
            [{"text": "🔭 Радар угод", "callback_data": "menu:scanner_radar"}],
            [{"text": "🧩 Пропущені угоди", "callback_data": "menu:skips"}],
            [{"text": "⬅️ Назад", "callback_data": "menu:main"}],
        ]
    }


def skips_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🕘 Останні", "callback_data": "menu:skips_last"}, {"text": "🔍 Review зараз", "callback_data": "menu:skips_review"}],
            [{"text": "🔴 Повний мінус", "callback_data": "menu:skips_loss"}],
            [{"text": "⚪ Біля нуля", "callback_data": "menu:skips_flat"}, {"text": "🟢 Макс плюс", "callback_data": "menu:skips_win"}],
            [{"text": "⏳ Ще не закрились", "callback_data": "menu:skips_pending"}],
            [{"text": "⬅️ Назад", "callback_data": "menu:main"}],
        ]
    }


def journal_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🔄 Оновити журнал", "callback_data": "menu:journal"}],
            [{"text": "💼 Мої позиції Polymarket", "callback_data": "menu:positions"}],
            [{"text": "🧾 Як закрити угоду", "callback_data": "menu:journal_help"}],
            [{"text": "⬅️ Назад", "callback_data": "menu:main"}],
        ]
    }


def positions_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🔄 Оновити позиції", "callback_data": "menu:positions"}],
            [{"text": "📒 Журнал", "callback_data": "menu:journal"}],
            [{"text": "⬅️ Назад", "callback_data": "menu:main"}],
        ]
    }


def main_menu_response() -> TelegramResponse:
    return TelegramResponse(
        "🏠 <b>Головне меню</b>\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "Обери розділ нижче. Кнопками швидше і менше плутанини, а ручні команди теж залишаються доступними.",
        reply_markup=main_menu_keyboard(),
        html=True,
    )


def entered_keyboard(signal_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "✅ Зайшов в угоду", "callback_data": f"entered:{signal_id}"}]]}


def handle_text_command(text: str) -> TelegramResponse:
    if text in {"/start", "/menu"}:
        return main_menu_response()
    if text == "/help":
        return TelegramResponse(render_help_card(), reply_markup=main_menu_keyboard(), html=True)
    if text == "/ping":
        return TelegramResponse("🟢 <b>pong</b>\n\nTelegram listener працює.", reply_markup=bot_menu_keyboard(), html=True)
    if text == "/status":
        return TelegramResponse(render_scanner_status(), reply_markup=bot_menu_keyboard(), html=True)
    if text == "/radar":
        return TelegramResponse(render_radar_status(), reply_markup=scanner_menu_keyboard(), html=True)
    if text in {"/why_no_signals", "/why"}:
        return TelegramResponse(render_why_no_signals(), reply_markup=scanner_menu_keyboard(), html=True)
    if text == "/last_skips":
        return TelegramResponse(render_last_skips(), reply_markup=skips_menu_keyboard(), html=True)
    if text == "/review_skips":
        return TelegramResponse(review_skips(), reply_markup=skips_menu_keyboard(), html=True)
    if text == "/journal":
        return TelegramResponse(render_journal_card(), reply_markup=journal_menu_keyboard(), html=True)
    if text.startswith("/positions"):
        return TelegramResponse(
            render_wallet_positions(wallet_from_text(text)),
            reply_markup=positions_menu_keyboard(),
            html=True,
        )
    if text.startswith("/close"):
        return TelegramResponse(render_close_command(text), reply_markup=journal_menu_keyboard(), html=True)

    try:
        argv = telegram_text_to_cli_args(text)
    except ValueError as exc:
        return TelegramResponse(
            f"⚠️ <b>Команда не розпізнана</b>\n\n{html.escape(str(exc))}\n\nВідкрий /menu, там усе під кнопками.",
            reply_markup=main_menu_keyboard(),
            html=True,
        )

    if not argv:
        return main_menu_response()

    if argv[0] == "analyze":
        return run_analyze_with_buttons(argv)
    if argv[0] == "scout":
        return run_scout_with_buttons(argv)
    return TelegramResponse(f"🧾 <b>Результат команди</b>\n━━━━━━━━━━━━━━━━\n\n{html.escape(run_cli(argv))}", html=True)


def handle_menu_callback(data: str) -> TelegramResponse:
    action = data.split(":", 1)[1]
    if action == "main":
        return main_menu_response()
    if action == "bot":
        return TelegramResponse(
            "🤖 <b>Бот</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Операційна панель для VPS-бота: статус, ping, restart і stop-підказки.",
            reply_markup=bot_menu_keyboard(),
            html=True,
        )
    if action == "bot_status":
        return TelegramResponse(render_scanner_status(), reply_markup=bot_menu_keyboard(), html=True)
    if action == "bot_ping":
        return TelegramResponse("🟢 <b>pong</b>\n\nTelegram listener відповідає.", reply_markup=bot_menu_keyboard(), html=True)
    if action == "bot_restart":
        return TelegramResponse(
            "🔄 <b>Рестарт бота</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Безпечний варіант зараз — через VPS або GitHub auto-deploy.\n\n"
            "VPS команда:\n<code>systemctl restart polymarket-bot</code>\n\n"
            "GitHub варіант: зроби <code>git push</code>, і Actions сам оновить та перезапустить сервіс.",
            reply_markup=bot_menu_keyboard(),
            html=True,
        )
    if action == "bot_stop":
        return TelegramResponse(
            "⏸ <b>Стоп бота</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Я не ставлю прямий stop без підтвердження, щоб випадково не вимкнути 24/7 monitoring.\n\n"
            "Зупинити:\n<code>systemctl stop polymarket-bot</code>\n\n"
            "Запустити назад:\n<code>systemctl start polymarket-bot</code>",
            reply_markup=bot_menu_keyboard(),
            html=True,
        )
    if action == "scanner":
        return TelegramResponse(
            "🔎 <b>Сканер</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Тут зібрані дії для live scanner: статус, пропущені угоди та review результатів.",
            reply_markup=scanner_menu_keyboard(),
            html=True,
        )
    if action == "scanner_status":
        return TelegramResponse(render_scanner_status(), reply_markup=scanner_menu_keyboard(), html=True)
    if action == "scanner_radar":
        return TelegramResponse(render_radar_status(), reply_markup=scanner_menu_keyboard(), html=True)
    if action == "scanner_why":
        return TelegramResponse(render_why_no_signals(), reply_markup=scanner_menu_keyboard(), html=True)
    if action == "skips":
        return TelegramResponse(
            "🧩 <b>Пропущені угоди</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Тут бот збирає угоди, які ми не взяли. Після дедлайну він перевіряє, чи були вони прибутковими, і розкладає їх по категоріях.",
            reply_markup=skips_menu_keyboard(),
            html=True,
        )
    if action == "skips_last":
        return TelegramResponse(render_last_skips(), reply_markup=skips_menu_keyboard(), html=True)
    if action == "skips_review":
        return TelegramResponse(review_skips(), reply_markup=skips_menu_keyboard(), html=True)
    if action == "skips_loss":
        return TelegramResponse(render_skips_bucket("loss"), reply_markup=skips_menu_keyboard(), html=True)
    if action == "skips_flat":
        return TelegramResponse(render_skips_bucket("flat"), reply_markup=skips_menu_keyboard(), html=True)
    if action == "skips_win":
        return TelegramResponse(render_skips_bucket("win"), reply_markup=skips_menu_keyboard(), html=True)
    if action == "skips_pending":
        return TelegramResponse(render_skips_bucket("pending"), reply_markup=skips_menu_keyboard(), html=True)
    if action == "journal":
        return TelegramResponse(render_journal_card(), reply_markup=journal_menu_keyboard(), html=True)
    if action == "positions":
        return TelegramResponse(render_wallet_positions(), reply_markup=positions_menu_keyboard(), html=True)
    if action == "journal_help":
        return TelegramResponse(
            "📒 <b>Як працює журнал</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Коли ти натискаєш <b>✅ Зайшов в угоду</b> під сигналом, бот записує її в журнал.\n\n"
            "Після закриття внеси результат:\n"
            "<code>/close trade_id --pnl 42.5 --note \"коментар\"</code>\n\n"
            "Так ми будемо бачити реальну статистику стратегії, а не тільки теоретичні сигнали.",
            reply_markup=journal_menu_keyboard(),
            html=True,
        )
    if action == "help":
        return TelegramResponse(render_help_card(), reply_markup=main_menu_keyboard(), html=True)
    return TelegramResponse("⚠️ Невідомий пункт меню.", reply_markup=main_menu_keyboard(), html=True)


def render_close_command(text: str) -> str:
    parser = argparse.ArgumentParser(prog="/close")
    parser.add_argument("command")
    parser.add_argument("trade_id")
    parser.add_argument("--pnl", type=float, required=True)
    parser.add_argument("--note")

    try:
        args = parser.parse_args(shlex.split(text))
        trade = close_trade(args.trade_id, args.pnl, args.note)
    except SystemExit:
        return (
            "⚠️ <b>Не вистачає даних</b>\n\n"
            "Формат:\n<code>/close &lt;trade_id&gt; --pnl &lt;amount&gt; --note \"optional note\"</code>"
        )
    except Exception as exc:
        return f"⚠️ <b>Не вдалося закрити угоду</b>\n\n{html.escape(str(exc))}"

    return (
        "✅ <b>Угоду закрито</b>\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"• Trade ID: <code>{html.escape(trade.trade_id)}</code>\n"
        f"• Ринок: {html.escape(trade.title)}\n"
        f"• Realized PnL: <b>{money(trade.realized_pnl or 0.0)}</b>\n\n"
        f"{render_journal_card()}"
    )


def _pretty_handle_callback(self: TelegramBot, callback: dict[str, Any]) -> None:
    data = str(callback.get("data") or "")
    callback_id = str(callback.get("id") or "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))

    if self.allowed_chat_id and chat_id != self.allowed_chat_id:
        self.answer_callback(callback_id, "Access denied")
        return

    if data.startswith("menu:"):
        self.answer_callback(callback_id, "OK")
        self.send_report(chat_id, handle_menu_callback(data))
        return

    if data.startswith("entered:"):
        signal_id = data.split(":", 1)[1]
        try:
            trade = record_entry(signal_id)
        except Exception as exc:
            self.answer_callback(callback_id, f"Помилка: {exc}")
            return
        self.answer_callback(callback_id, "Записано в журнал")
        self.send_report(
            chat_id,
            TelegramResponse(
                text=(
                    "✅ <b>Угоду записано в журнал</b>\n"
                    "━━━━━━━━━━━━━━━━\n\n"
                    f"• Trade ID: <code>{html.escape(trade.trade_id)}</code>\n"
                    f"• Сигнал: {html.escape(trade.title)}\n"
                    f"• Рішення на вході: <b>{html.escape(trade.decision)}</b>\n"
                    f"• Ймовірність позитивного результату: <b>{trade.positive_probability * 100:.1f}%</b>\n\n"
                    "Після закриття внеси результат командою:\n"
                    f"<code>/close {html.escape(trade.trade_id)} --pnl 42.5 --note \"коментар\"</code>"
                ),
                reply_markup=journal_menu_keyboard(),
                html=True,
            ),
        )
        return

    self.answer_callback(callback_id, "Невідома дія")


TelegramBot.handle_callback = _pretty_handle_callback


def run_scout_with_buttons(argv: list[str]) -> TelegramResponse:
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    config = cli.build_config(args)
    candidates = load_candidates(args.candidates, default_stake=args.stake)
    opportunities = scout_candidates(
        candidates,
        config,
        max_futures_margin=args.max_futures_margin,
        use_live_orderbook=args.live_orderbook,
        max_slippage=args.max_slippage,
    )
    shown = opportunities[: args.top]
    text = render_scout_cards(opportunities, args.top)
    buttons = []
    for index, opportunity in enumerate(shown, start=1):
        signal = create_signal(
            kind="scout",
            title=opportunity.candidate.slug,
            decision=opportunity.decision,
            positive_probability=positive_result_probability(opportunity.edge, opportunity.costs),
            payload={
                "command": argv,
                "rank": index,
                "slug": opportunity.candidate.slug,
                "stake": opportunity.candidate.stake,
                "decision": opportunity.decision,
                "edge": opportunity.edge.true_edge,
                "positive_probability": positive_result_probability(opportunity.edge, opportunity.costs),
                "futures_side": opportunity.hedge.side,
                "futures_size_btc": opportunity.hedge.size_btc,
                "futures_leverage": opportunity.hedge.leverage,
                "worst_case_after_sl": opportunity.worst_case_after_sl,
            },
        )
        buttons.append([{"text": f"✅ Зайшов #{index}", "callback_data": f"entered:{signal.signal_id}"}])
    return TelegramResponse(text=text, reply_markup={"inline_keyboard": buttons} if buttons else None, html=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polymarket-telegram-bot")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--token", help="Telegram bot token. Defaults to TELEGRAM_BOT_TOKEN")
    parser.add_argument("--allowed-chat-id", help="Optional chat allowlist. Defaults to TELEGRAM_ALLOWED_CHAT_ID")
    parser.add_argument("--dry-run", help="Process one Telegram-style command without connecting to Telegram")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    load_dotenv(args.env_file)

    if args.dry_run:
        safe_print(handle_text_command(args.dry_run).text)
        return 0

    token = args.token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing. Add it to .env or pass --token.")

    allowed_chat_id = args.allowed_chat_id or os.environ.get("TELEGRAM_ALLOWED_CHAT_ID")
    bot = TelegramBot(token=token, allowed_chat_id=allowed_chat_id)
    bot.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
