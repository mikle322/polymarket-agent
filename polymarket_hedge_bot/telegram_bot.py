import argparse
import html
import io
import json
import os
import shlex
import time
from dataclasses import dataclass
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from polymarket_hedge_bot import cli
from polymarket_hedge_bot.config import RiskConfig
from polymarket_hedge_bot.costs import calculate_costs
from polymarket_hedge_bot.edge import calculate_edge
from polymarket_hedge_bot.formatting import money, positive_result_probability
from polymarket_hedge_bot.hedge import calculate_futures_hedge
from polymarket_hedge_bot.journal import (
    clear_futures_leg,
    close_trade,
    create_manual_trade,
    create_signal,
    journal_summary,
    record_polymarket_position,
    record_entry,
    update_futures_leg,
    update_pm_leg,
    update_trade_payload,
)
from polymarket_hedge_bot.liquidity import check_basic_liquidity
from polymarket_hedge_bot.opportunity_history import render_history_summary
from polymarket_hedge_bot.paper_trading import render_paper_review_summary, render_paper_summary, review_due_paper_trades
from polymarket_hedge_bot.position_monitor import render_position_monitor_status
from polymarket_hedge_bot.connectors.polymarket_data import PolymarketDataConnector
from polymarket_hedge_bot.positions import render_position_risk_summary, render_wallet_positions, wallet_from_text
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


JOURNAL_PM_POSITIONS_PATH = Path("data") / "journal_polymarket_positions.json"


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
            "payout_multiple": costs.payout_multiple,
            "net_no_win_flat": costs.net_no_win_flat,
            "net_touch_with_hedge_tp": costs.net_touch_with_hedge_tp,
            "net_no_win_after_hedge_sl": costs.net_no_win_after_hedge_sl,
            "net_touch_after_hedge_sl_loss": costs.net_touch_after_hedge_sl_loss,
            "touch_break_even_price": costs.touch_break_even_price,
            "no_win_after_sl_break_even_price": costs.no_win_after_sl_break_even_price,
            "no_exit_break_even_price": costs.no_exit_break_even_price,
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
                "payout_multiple": opportunity.costs.payout_multiple,
                "net_no_win_flat": opportunity.costs.net_no_win_flat,
                "net_touch_with_hedge_tp": opportunity.costs.net_touch_with_hedge_tp,
                "net_no_win_after_hedge_sl": opportunity.costs.net_no_win_after_hedge_sl,
                "net_touch_after_hedge_sl_loss": opportunity.costs.net_touch_after_hedge_sl_loss,
                "touch_break_even_price": opportunity.costs.touch_break_even_price,
                "no_win_after_sl_break_even_price": opportunity.costs.no_win_after_sl_break_even_price,
                "no_exit_break_even_price": opportunity.costs.no_exit_break_even_price,
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
    from polymarket_hedge_bot.journal import calculate_total_pnl, load_trades

    trades = load_trades()
    open_trades = [trade for trade in trades if trade.status == "OPEN"]
    closed = [trade for trade in trades if trade.status == "CLOSED"]
    realized = [trade.realized_pnl for trade in closed if trade.realized_pnl is not None]
    open_leg_pnl = sum(calculate_total_pnl(trade) for trade in open_trades)
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
        f"• Поточний PnL по відкритих ногах: <b>{money(open_leg_pnl)}</b>",
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
        leg_total = calculate_total_pnl(trade)
        pnl = f" | PnL <b>{money(trade.realized_pnl)}</b>" if trade.realized_pnl is not None else f" | legs <b>{money(leg_total)}</b>"
        payload = trade.payload or {}
        pm_line = ""
        if payload.get("pm_price") is not None:
            pm_line = (
                f"• PM: {html.escape(str(payload.get('pm_side', 'BUY')))} "
                f"{html.escape(str(payload.get('pm_outcome', '')))} "
                f"{float(payload.get('pm_shares', 0.0)):.2f} @ {float(payload.get('pm_price', 0.0)):.3f} "
                f"| cost {money(float(payload.get('pm_cost', 0.0)))}"
            )
            if payload.get("pm_pnl") is not None:
                pm_line += f" | PnL {money(float(payload.get('pm_pnl', 0.0)))}"
            if payload.get("pm_current_value") is not None:
                pm_line += f" | value {money(float(payload.get('pm_current_value', 0.0)))}"
        futures_line = ""
        if payload.get("futures_entry_price") is not None:
            futures_line = (
                f"• Futures: {html.escape(str(payload.get('futures_side', '')))} "
                f"{float(payload.get('futures_size_btc', 0.0)):.6f} BTC @ {money(float(payload.get('futures_entry_price', 0.0)))}"
            )
            if payload.get("futures_exit_price") is not None:
                futures_line += f" → {money(float(payload.get('futures_exit_price', 0.0)))}"
            if payload.get("futures_pnl") is not None:
                futures_line += f" | PnL {money(float(payload.get('futures_pnl', 0.0)))}"
        lines.extend(
            [
                "",
                f"{icon} <code>{html.escape(trade.trade_id)}</code> | <b>{html.escape(trade.status)}</b>",
                f"• {html.escape(trade.decision)} | шанс: <b>{trade.positive_probability * 100:.1f}%</b>{pnl}",
                f"• {html.escape(trade.title)}",
            ]
        )
        if pm_line:
            lines.append(pm_line)
        if futures_line:
            lines.append(futures_line)
        if payload.get("pm_pnl") is not None or payload.get("futures_pnl") is not None:
            lines.append(f"• Разом по ногах: <b>{money(leg_total)}</b>")
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
            [{"text": "📚 Scanner history", "callback_data": "menu:scanner_history"}],
            [{"text": "🧪 Paper trading", "callback_data": "menu:paper"}],
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
            [{"text": "🟦 Оновити PM PnL", "callback_data": "menu:journal_sync_pm"}],
            [{"text": "➕ Добавити", "callback_data": "menu:journal_add"}],
            [{"text": "💼 Мої позиції Polymarket", "callback_data": "menu:positions"}],
            [{"text": "🧾 Як закрити угоду", "callback_data": "menu:journal_help"}],
            [{"text": "⬅️ Назад", "callback_data": "menu:main"}],
        ]
    }


def journal_add_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🆕 Створити угоду", "callback_data": "menu:journal_add_trade"}],
            [{"text": "🟦 Додати Polymarket", "callback_data": "menu:journal_add_pm"}],
            [{"text": "📉 Додати Futures", "callback_data": "menu:journal_add_futures"}],
            [{"text": "🧹 Видалити Futures", "callback_data": "menu:journal_clear_futures"}],
            [{"text": "✅ Закрити угоду", "callback_data": "menu:journal_add_close"}],
            [{"text": "⬅️ Журнал", "callback_data": "menu:journal"}],
        ]
    }


def journal_polymarket_positions_keyboard(positions: list[Any]) -> dict[str, Any]:
    buttons = []
    for index, _position in enumerate(positions[:5], start=1):
        buttons.append([{"text": f"➕ Додати #{index}", "callback_data": f"pmpos:{index - 1}"}])
    buttons.append([{"text": "⬅️ Добавити", "callback_data": "menu:journal_add"}])
    return {"inline_keyboard": buttons}


def render_journal_add_polymarket_positions(limit: int = 5, timeout: float = 8.0) -> TelegramResponse:
    wallet = os.environ.get("POLYMARKET_WALLET_ADDRESS") or os.environ.get("POLYMARKET_PROXY_WALLET")
    if not wallet:
        return TelegramResponse(
            "⚠️ <b>Не бачу Polymarket wallet</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Додай у <code>.env</code> публічну адресу:\n"
            "<code>POLYMARKET_WALLET_ADDRESS=0x...</code>\n\n"
            "Private key для журналу не потрібен.",
            reply_markup=journal_add_menu_keyboard(),
            html=True,
        )

    connector = PolymarketDataConnector(timeout=timeout)
    try:
        positions, checked_wallets, proxy_wallet = load_recent_polymarket_positions(connector, wallet, limit)
    except Exception as exc:
        return TelegramResponse(
            "⚠️ <b>Не вдалося підтягнути угоди Polymarket</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"Причина: <code>{html.escape(str(exc))}</code>",
            reply_markup=journal_add_menu_keyboard(),
            html=True,
        )

    save_journal_pm_positions(positions)
    if not positions:
        checked = ", ".join(short_wallet(item) for item in checked_wallets) if checked_wallets else short_wallet(wallet)
        return TelegramResponse(
            "🟦 <b>Додати Polymarket</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"Перевірив wallet: <code>{html.escape(checked)}</code>\n"
            "Останніх позицій не знайдено. Якщо угоди точно є, найімовірніше потрібна proxy wallet адреса з профілю Polymarket.",
            reply_markup=journal_add_menu_keyboard(),
            html=True,
        )

    lines = [
        "🟦 <b>Додати Polymarket</b>",
        "━━━━━━━━━━━━━━━━",
        f"Wallet: <code>{html.escape(short_wallet(wallet))}</code>",
        f"Checked: <code>{html.escape(', '.join(short_wallet(item) for item in checked_wallets))}</code>",
    ]
    if proxy_wallet:
        lines.append(f"Proxy: <code>{html.escape(short_wallet(proxy_wallet))}</code>")
    lines.extend(["", f"Останні {len(positions)} Polymarket fills/позицій. Натисни кнопку під потрібною, і я внесу її в журнал."])

    for index, position in enumerate(positions, start=1):
        price = polymarket_position_price(position)
        cost = polymarket_position_cost(position)
        pnl = polymarket_position_pnl(position)
        status = polymarket_position_status(position)
        lines.extend(
            [
                "",
                f"<b>#{index}</b> {html.escape(status)} | {html.escape(polymarket_position_outcome(position))} "
                f"| {polymarket_position_size(position):.2f} @ {price:.3f}",
                f"<code>{html.escape(polymarket_position_slug(position))}</code>",
                f"Cost: <b>{money(cost)}</b> | Value: <b>{money(polymarket_position_current_value(position))}</b> | PnL: <b>{money(pnl)}</b>",
            ]
        )

    return TelegramResponse(
        "\n".join(lines),
        reply_markup=journal_polymarket_positions_keyboard(positions),
        html=True,
    )


def load_recent_polymarket_positions(
    connector: PolymarketDataConnector,
    wallet: str,
    limit: int,
) -> tuple[list[Any], list[str], str | None]:
    checked_wallets: list[str] = []
    proxy_wallet: str | None = None
    try:
        proxy_wallet = connector.get_proxy_wallet(wallet)
    except Exception:
        proxy_wallet = None

    wallets = [item for item in [proxy_wallet, wallet] if item]
    seen_wallets: set[str] = set()
    all_positions: list[Any] = []
    seen_positions: set[str] = set()
    for candidate_wallet in wallets:
        normalized_wallet = candidate_wallet.lower()
        if normalized_wallet in seen_wallets:
            continue
        seen_wallets.add(normalized_wallet)
        checked_wallets.append(candidate_wallet)
        try:
            activities = connector.get_activity(candidate_wallet, limit=max(limit, 25), activity_type="TRADE")
        except Exception:
            activities = []
        for activity in activities:
            key = str(
                activity.get("transactionHash")
                or activity.get("txHash")
                or activity.get("orderHash")
                or activity.get("asset")
                or f"{activity.get('conditionId')}:{activity.get('outcome')}:{activity.get('timestamp')}"
            )
            if key in seen_positions:
                continue
            seen_positions.add(key)
            all_positions.append(activity)

    if all_positions:
        all_positions.sort(key=polymarket_position_sort_value, reverse=True)
        return all_positions[:limit], checked_wallets, proxy_wallet

    for candidate_wallet in wallets:
        positions = connector.get_positions(
            candidate_wallet,
            limit=max(limit, 25),
            size_threshold=0.0,
            sort_by="CURRENT",
            sort_direction="DESC",
        )
        for position in positions:
            key = position.asset or f"{position.condition_id}:{position.outcome_index}"
            if key in seen_positions:
                continue
            seen_positions.add(key)
            all_positions.append(position)

    all_positions.sort(key=polymarket_position_sort_value, reverse=True)
    return all_positions[:limit], checked_wallets, proxy_wallet


def load_polymarket_positions_for_journal(
    connector: PolymarketDataConnector,
    wallet: str,
    limit: int = 200,
) -> tuple[list[Any], list[str], str | None]:
    checked_wallets: list[str] = []
    proxy_wallet: str | None = None
    try:
        proxy_wallet = connector.get_proxy_wallet(wallet)
    except Exception:
        proxy_wallet = None

    wallets = [item for item in [proxy_wallet, wallet] if item]
    positions: list[Any] = []
    seen_wallets: set[str] = set()
    seen_positions: set[str] = set()
    for candidate_wallet in wallets:
        normalized_wallet = candidate_wallet.lower()
        if normalized_wallet in seen_wallets:
            continue
        seen_wallets.add(normalized_wallet)
        checked_wallets.append(candidate_wallet)
        for position in connector.get_positions(
            candidate_wallet,
            limit=limit,
            size_threshold=0.0,
            sort_by="CURRENT",
            sort_direction="DESC",
        ):
            key = position.asset or f"{position.condition_id}:{position.outcome_index}"
            if key in seen_positions:
                continue
            seen_positions.add(key)
            positions.append(position)
    return positions, checked_wallets, proxy_wallet


def load_polymarket_activities_for_journal(
    connector: PolymarketDataConnector,
    wallet: str,
    limit: int = 200,
) -> tuple[list[dict[str, Any]], list[str], str | None]:
    checked_wallets: list[str] = []
    proxy_wallet: str | None = None
    try:
        proxy_wallet = connector.get_proxy_wallet(wallet)
    except Exception:
        proxy_wallet = None

    wallets = [item for item in [proxy_wallet, wallet] if item]
    activities: list[dict[str, Any]] = []
    seen_wallets: set[str] = set()
    seen_activities: set[str] = set()
    for candidate_wallet in wallets:
        normalized_wallet = candidate_wallet.lower()
        if normalized_wallet in seen_wallets:
            continue
        seen_wallets.add(normalized_wallet)
        checked_wallets.append(candidate_wallet)
        for activity in connector.get_activity(candidate_wallet, limit=limit, activity_type="TRADE"):
            key = str(
                activity.get("transactionHash")
                or activity.get("txHash")
                or activity.get("orderHash")
                or f"{activity.get('conditionId')}:{activity.get('asset')}:{activity.get('side')}:{activity.get('timestamp')}"
            )
            if key in seen_activities:
                continue
            seen_activities.add(key)
            activities.append(activity)
    return activities, checked_wallets, proxy_wallet


def sync_journal_polymarket_response() -> TelegramResponse:
    from polymarket_hedge_bot.journal import load_trades

    wallet = os.environ.get("POLYMARKET_WALLET_ADDRESS") or os.environ.get("POLYMARKET_PROXY_WALLET")
    if not wallet:
        return TelegramResponse(
            "⚠️ <b>Не бачу Polymarket wallet</b>\n\n"
            "Додай у <code>.env</code>:\n<code>POLYMARKET_WALLET_ADDRESS=0x...</code>",
            reply_markup=journal_menu_keyboard(),
            html=True,
        )

    connector = PolymarketDataConnector(timeout=8)
    try:
        positions, checked_wallets, proxy_wallet = load_polymarket_positions_for_journal(connector, wallet)
        activities, activity_wallets, activity_proxy_wallet = load_polymarket_activities_for_journal(connector, wallet)
        checked_wallets = list(dict.fromkeys([*checked_wallets, *activity_wallets]))
        proxy_wallet = proxy_wallet or activity_proxy_wallet
    except Exception as exc:
        return TelegramResponse(
            "⚠️ <b>Не вдалося оновити PM PnL</b>\n\n"
            f"Причина: <code>{html.escape(str(exc))}</code>",
            reply_markup=journal_menu_keyboard(),
            html=True,
        )

    updated = 0
    skipped = 0
    for trade in load_trades():
        payload = trade.payload or {}
        if payload.get("pm_price") is None:
            continue
        close_activity = find_matching_polymarket_close_activity(trade, activities)
        if close_activity is not None:
            proceeds = polymarket_activity_value(close_activity)
            pm_cost = float(payload.get("pm_cost") or 0.0)
            update_trade_payload(
                trade.trade_id,
                {
                    "pm_slug": polymarket_activity_slug(close_activity),
                    "pm_current_value": proceeds,
                    "pm_pnl": proceeds - pm_cost,
                    "pm_status": f"CLOSED SELL {polymarket_activity_price(close_activity):.3f}",
                    "pm_closed_value": proceeds,
                },
            )
            updated += 1
            continue
        position = find_matching_polymarket_position(trade, positions)
        if position is None:
            skipped += 1
            continue
        update_trade_payload(
            trade.trade_id,
            {
                "pm_slug": polymarket_position_slug(position),
                "pm_current_value": polymarket_position_current_value(position),
                "pm_pnl": polymarket_position_pnl(position),
                "pm_status": polymarket_position_status(position),
            },
        )
        updated += 1

    checked = ", ".join(short_wallet(item) for item in checked_wallets) if checked_wallets else short_wallet(wallet)
    lines = [
        "🟦 <b>PM PnL оновлено</b>",
        "━━━━━━━━━━━━━━━━",
        f"• Оновлено угод: <b>{updated}</b>",
        f"• Не знайшов збіг: <b>{skipped}</b>",
        f"• Checked: <code>{html.escape(checked)}</code>",
    ]
    if proxy_wallet:
        lines.append(f"• Proxy: <code>{html.escape(short_wallet(proxy_wallet))}</code>")
    lines.extend(["", render_journal_card()])
    return TelegramResponse("\n".join(lines), reply_markup=journal_menu_keyboard(), html=True)


def find_matching_polymarket_position(trade: Any, positions: list[Any]) -> Any | None:
    payload = trade.payload or {}
    trade_slug = normalize_match_text(str(payload.get("pm_slug") or ""))
    trade_title = normalize_match_text(str(trade.title or ""))
    trade_outcome = normalize_match_text(str(payload.get("pm_outcome") or ""))

    best_position = None
    best_score = 0
    for position in positions:
        position_slug = normalize_match_text(polymarket_position_slug(position))
        position_title = normalize_match_text(polymarket_position_title(position))
        position_outcome = normalize_match_text(polymarket_position_outcome(position))
        score = 0
        if trade_slug and trade_slug == position_slug:
            score += 6
        if trade_title and trade_title == position_title:
            score += 5
        elif trade_title and (trade_title in position_title or position_title in trade_title):
            score += 3
        if trade_outcome and trade_outcome == position_outcome:
            score += 2
        if score > best_score:
            best_score = score
            best_position = position
    return best_position if best_score >= 3 else None


def find_matching_polymarket_close_activity(trade: Any, activities: list[dict[str, Any]]) -> dict[str, Any] | None:
    payload = trade.payload or {}
    trade_slug = normalize_match_text(str(payload.get("pm_slug") or ""))
    trade_title = normalize_match_text(str(trade.title or ""))
    trade_outcome = normalize_match_text(str(payload.get("pm_outcome") or ""))

    matches: list[tuple[int, float, dict[str, Any]]] = []
    for activity in activities:
        if polymarket_activity_side(activity) != "SELL":
            continue
        value = polymarket_activity_value(activity)
        if value <= 0:
            continue
        activity_slug = normalize_match_text(polymarket_activity_slug(activity))
        activity_title = normalize_match_text(polymarket_activity_title(activity))
        activity_outcome = normalize_match_text(polymarket_activity_outcome(activity))
        score = 0
        if trade_slug and trade_slug == activity_slug:
            score += 6
        if trade_title and trade_title == activity_title:
            score += 5
        elif trade_title and (trade_title in activity_title or activity_title in trade_title):
            score += 3
        if trade_outcome and trade_outcome == activity_outcome:
            score += 2
        if score >= 3:
            matches.append((score, polymarket_activity_timestamp(activity), activity))

    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return matches[0][2]


def polymarket_activity_side(activity: dict[str, Any]) -> str:
    return str(activity.get("side") or activity.get("transactionType") or activity.get("type") or "").upper()


def polymarket_activity_value(activity: dict[str, Any]) -> float:
    for key in ("usdcSize", "value", "cashAmount", "collateralAmount", "quoteAmount", "proceeds"):
        value = activity.get(key)
        if value is not None:
            return abs(float(value))
    price = polymarket_activity_price(activity)
    size = float(activity.get("size") or activity.get("shares") or 0.0)
    return abs(price * size)


def polymarket_activity_price(activity: dict[str, Any]) -> float:
    return float(activity.get("price") or activity.get("avgPrice") or 0.0)


def polymarket_activity_timestamp(activity: dict[str, Any]) -> float:
    return float(activity.get("timestamp") or activity.get("createdAt") or activity.get("updatedAt") or 0.0)


def polymarket_activity_outcome(activity: dict[str, Any]) -> str:
    return str(activity.get("outcome") or activity.get("outcomeName") or "YES")


def polymarket_activity_title(activity: dict[str, Any]) -> str:
    return str(activity.get("title") or activity.get("marketTitle") or activity.get("slug") or activity.get("conditionId") or "")


def polymarket_activity_slug(activity: dict[str, Any]) -> str:
    return str(activity.get("slug") or activity.get("marketSlug") or activity.get("conditionId") or "")


def normalize_match_text(value: str) -> str:
    return " ".join(value.lower().replace("-", " ").replace("_", " ").split())


def save_journal_pm_positions(positions: list[Any]) -> None:
    JOURNAL_PM_POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = [serialize_polymarket_position(position) for position in positions[:5]]
    JOURNAL_PM_POSITIONS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_journal_pm_position(index: int) -> dict[str, Any]:
    if not JOURNAL_PM_POSITIONS_PATH.exists():
        raise ValueError("список Polymarket позицій застарів. Натисни 'Додати Polymarket' ще раз")
    positions = json.loads(JOURNAL_PM_POSITIONS_PATH.read_text(encoding="utf-8"))
    if index < 0 or index >= len(positions):
        raise ValueError("позицію не знайдено. Онови список і спробуй ще раз")
    return positions[index]


def serialize_polymarket_position(position: Any) -> dict[str, Any]:
    price = polymarket_position_price(position)
    cost = polymarket_position_cost(position)
    pnl = polymarket_position_pnl(position)
    return {
        "title": polymarket_position_title(position),
        "slug": polymarket_position_slug(position),
        "outcome": polymarket_position_outcome(position),
        "price": price,
        "shares": polymarket_position_size(position),
        "cost": cost,
        "pnl": pnl,
        "current_value": polymarket_position_current_value(position),
        "status": polymarket_position_status(position),
    }


def handle_polymarket_position_callback(data: str) -> TelegramResponse:
    index = int(data.split(":", 1)[1])
    position = load_journal_pm_position(index)
    trade = record_polymarket_position(
        title=str(position.get("title") or position.get("slug") or "Polymarket position"),
        outcome=str(position.get("outcome") or "YES"),
        price=float(position.get("price") or 0.0),
        shares=float(position.get("shares") or 0.0),
        cost=float(position.get("cost") or 0.0),
        pnl=float(position.get("pnl") or 0.0),
        slug=str(position.get("slug") or ""),
        current_value=float(position.get("current_value") or 0.0),
        status=str(position.get("status") or ""),
    )
    return TelegramResponse(
        "✅ <b>Polymarket позицію додано в журнал</b>\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"• Джерело: <code>{html.escape(str(position.get('slug') or position.get('title') or 'Polymarket'))}</code>\n"
        f"• Статус на Polymarket: <b>{html.escape(str(position.get('status') or ''))}</b>\n\n"
        + render_trade_line(trade),
        reply_markup=journal_menu_keyboard(),
        html=True,
    )


def clear_futures_keyboard(trades: list[Any]) -> dict[str, Any]:
    buttons = []
    for trade in trades[:10]:
        label = f"🧹 {trade.trade_id}"
        buttons.append([{"text": label, "callback_data": f"clearfut:{trade.trade_id}"}])
    buttons.append([{"text": "⬅️ Добавити", "callback_data": "menu:journal_add"}])
    return {"inline_keyboard": buttons}


def render_clear_futures_picker(limit: int = 10) -> TelegramResponse:
    from polymarket_hedge_bot.journal import load_trades

    trades = [
        trade
        for trade in reversed(load_trades())
        if (trade.payload or {}).get("futures_entry_price") is not None
    ][:limit]
    if not trades:
        return TelegramResponse(
            "🧹 <b>Видалити Futures</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "У журналі поки немає угод із внесеною futures-ногою.",
            reply_markup=journal_add_menu_keyboard(),
            html=True,
        )

    lines = [
        "🧹 <b>Видалити Futures</b>",
        "━━━━━━━━━━━━━━━━",
        "Обери угоду, з якої треба прибрати futures-ногу. Polymarket-частина залишиться в журналі.",
    ]
    for index, trade in enumerate(trades, start=1):
        payload = trade.payload or {}
        lines.extend(
            [
                "",
                f"<b>#{index}</b> <code>{html.escape(trade.trade_id)}</code> | {html.escape(trade.status)}",
                f"• {html.escape(trade.title)}",
                f"• Futures: {html.escape(str(payload.get('futures_side', '')))} "
                f"{float(payload.get('futures_size_btc', 0.0)):.6f} BTC @ {money(float(payload.get('futures_entry_price', 0.0)))}",
            ]
        )
    return TelegramResponse("\n".join(lines), reply_markup=clear_futures_keyboard(trades), html=True)


def handle_clear_futures_callback(data: str) -> TelegramResponse:
    trade_id = data.split(":", 1)[1]
    trade = clear_futures_leg(trade_id)
    return TelegramResponse(
        "✅ <b>Futures-ногу видалено</b>\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "Тепер можеш внести правильну futures-угоду заново.\n\n"
        + render_trade_line(trade),
        reply_markup=journal_menu_keyboard(),
        html=True,
    )


def polymarket_position_price(position: Any) -> float:
    if isinstance(position, dict):
        return float(position.get("price") or position.get("avgPrice") or position.get("curPrice") or 0.0)
    return float(position.avg_price or position.cur_price or 0.0)


def polymarket_position_cost(position: Any) -> float:
    if isinstance(position, dict):
        explicit_cost = position.get("usdcSize") or position.get("amount") or position.get("initialValue")
        return float(explicit_cost or (polymarket_position_price(position) * polymarket_position_size(position)))
    return float(position.initial_value or (polymarket_position_price(position) * position.size))


def polymarket_position_pnl(position: Any) -> float:
    if isinstance(position, dict):
        return float(position.get("realizedPnl") or position.get("cashPnl") or position.get("pnl") or 0.0)
    if position.current_value > 0:
        return float(position.cash_pnl)
    return float(position.realized_pnl or position.cash_pnl or 0.0)


def polymarket_position_sort_value(position: Any) -> float:
    if isinstance(position, dict):
        return float(position.get("timestamp") or position.get("createdAt") or position.get("updatedAt") or 0.0)
    return max(float(position.current_value or 0.0), float(position.initial_value or 0.0), float(position.total_bought or 0.0))


def polymarket_position_status(position: Any) -> str:
    if isinstance(position, dict):
        side = str(position.get("side") or position.get("transactionType") or "FILL").upper()
        return f"FILL {side}" if side != "FILL" else "FILL"
    if position.redeemable:
        return "CLOSED/REDEEMABLE"
    if position.current_value <= 0 and position.realized_pnl != 0:
        return "CLOSED"
    if position.size > 0:
        return "OPEN"
    return "UNKNOWN"


def polymarket_position_size(position: Any) -> float:
    if isinstance(position, dict):
        return float(position.get("size") or position.get("shares") or position.get("amount") or 0.0)
    return float(position.size or 0.0)


def polymarket_position_current_value(position: Any) -> float:
    if isinstance(position, dict):
        return float(position.get("currentValue") or 0.0)
    return float(position.current_value or 0.0)


def polymarket_position_outcome(position: Any) -> str:
    if isinstance(position, dict):
        return str(position.get("outcome") or position.get("outcomeName") or "YES")
    return str(position.outcome or "YES")


def polymarket_position_title(position: Any) -> str:
    if isinstance(position, dict):
        return str(position.get("title") or position.get("slug") or position.get("conditionId") or "Polymarket fill")
    return str(position.title or position.slug or position.condition_id)


def polymarket_position_slug(position: Any) -> str:
    if isinstance(position, dict):
        return str(position.get("slug") or position.get("marketSlug") or position.get("conditionId") or "polymarket-fill")
    return str(position.slug or position.condition_id[:12])


def short_wallet(wallet: str) -> str:
    if len(wallet) <= 12:
        return wallet
    return f"{wallet[:6]}...{wallet[-4:]}"


def positions_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🔄 Оновити позиції", "callback_data": "menu:positions"}],
            [{"text": "🧯 Risk summary", "callback_data": "menu:positions_risk"}],
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
    if text in {"/history", "/scanner_history"}:
        return TelegramResponse(render_history_summary(), reply_markup=scanner_menu_keyboard(), html=True)
    if text == "/paper":
        return TelegramResponse(render_paper_summary(), reply_markup=scanner_menu_keyboard(), html=True)
    if text == "/paper_review":
        return TelegramResponse(
            render_paper_review_summary(review_due_paper_trades()),
            reply_markup=scanner_menu_keyboard(),
            html=True,
        )
    if text in {"/fill_monitor", "/position_monitor"}:
        return TelegramResponse(render_position_monitor_status(), reply_markup=positions_menu_keyboard(), html=True)
    if text == "/last_skips":
        return TelegramResponse(render_last_skips(), reply_markup=skips_menu_keyboard(), html=True)
    if text == "/review_skips":
        return TelegramResponse(review_skips(), reply_markup=skips_menu_keyboard(), html=True)
    if text == "/journal":
        return TelegramResponse(render_journal_card(), reply_markup=journal_menu_keyboard(), html=True)
    if text in {"/sync_pm", "/sync_polymarket"}:
        return sync_journal_polymarket_response()
    if text.startswith("/trade"):
        return TelegramResponse(render_trade_command(text), reply_markup=journal_menu_keyboard(), html=True)
    if text.startswith("/pm_fill"):
        return TelegramResponse(render_pm_fill_command(text), reply_markup=journal_menu_keyboard(), html=True)
    if text.startswith("/futures"):
        return TelegramResponse(render_futures_command(text), reply_markup=journal_menu_keyboard(), html=True)
    if text.startswith("/clear_futures"):
        return TelegramResponse(render_clear_futures_command(text), reply_markup=journal_menu_keyboard(), html=True)
    if text.startswith("/positions"):
        return TelegramResponse(
            render_wallet_positions(wallet_from_text(text)),
            reply_markup=positions_menu_keyboard(),
            html=True,
        )
    if text.startswith("/risk"):
        return TelegramResponse(
            render_position_risk_summary(wallet_from_text(text)),
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
    if action == "scanner_history":
        return TelegramResponse(render_history_summary(), reply_markup=scanner_menu_keyboard(), html=True)
    if action == "paper":
        return TelegramResponse(render_paper_summary(), reply_markup=scanner_menu_keyboard(), html=True)
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
    if action == "journal_sync_pm":
        return sync_journal_polymarket_response()
    if action == "journal_add":
        return TelegramResponse(
            "➕ <b>Добавити в журнал</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Обери, що саме хочеш внести. Я покажу готовий шаблон команди, куди треба підставити свої числа.",
            reply_markup=journal_add_menu_keyboard(),
            html=True,
        )
    if action == "journal_add_trade":
        return TelegramResponse(
            "🆕 <b>Створити ручну угоду</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Скопіюй шаблон і заміни назву:\n\n"
            "<code>/trade --title \"BTC 85k May hedge\" --note \"optional\"</code>",
            reply_markup=journal_add_menu_keyboard(),
            html=True,
        )
    if action == "journal_add_pm":
        return render_journal_add_polymarket_positions()
    if action == "journal_add_futures":
        return TelegramResponse(
            "📉 <b>Додати Futures ногу</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Вхід:\n"
            "<code>/futures trade_id --side SHORT --size-btc 0.0375 --entry 78000</code>\n\n"
            "Закриття, PnL порахується автоматично:\n"
            "<code>/futures trade_id --side SHORT --size-btc 0.0375 --entry 78000 --exit 70000</code>\n\n"
            "Або вручну:\n"
            "<code>/futures trade_id --side SHORT --size-btc 0.0375 --entry 78000 --exit 70000 --pnl 300</code>",
            reply_markup=journal_add_menu_keyboard(),
            html=True,
        )
    if action == "journal_clear_futures":
        return render_clear_futures_picker()
    if action == "journal_add_close":
        return TelegramResponse(
            "✅ <b>Закрити угоду</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Якщо вже внесені <code>pm_pnl</code> і <code>futures_pnl</code>, бот складе їх сам:\n\n"
            "<code>/close trade_id</code>\n\n"
            "Або задай сумарний результат вручну:\n"
            "<code>/close trade_id --pnl 180 --note \"закрив руками\"</code>",
            reply_markup=journal_add_menu_keyboard(),
            html=True,
        )
    if action == "positions":
        return TelegramResponse(render_wallet_positions(), reply_markup=positions_menu_keyboard(), html=True)
    if action == "positions_risk":
        return TelegramResponse(render_position_risk_summary(), reply_markup=positions_menu_keyboard(), html=True)
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
    parser.add_argument("--pnl", type=float)
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


def render_trade_command(text: str) -> str:
    parser = argparse.ArgumentParser(prog="/trade")
    parser.add_argument("command")
    parser.add_argument("--title", required=True)
    parser.add_argument("--note")
    try:
        args = parser.parse_args(shlex.split(text))
        trade = create_manual_trade(args.title, args.note)
    except SystemExit:
        return "Формат:\n<code>/trade --title \"BTC 85k May hedge\" --note \"optional\"</code>"
    except Exception as exc:
        return f"⚠️ <b>Не вдалося створити угоду</b>\n\n{html.escape(str(exc))}"

    return (
        "✅ <b>Угоду створено</b>\n"
        "━━━━━━━━━━━━━━━━\n\n"
        f"• Trade ID: <code>{html.escape(trade.trade_id)}</code>\n"
        f"• Назва: {html.escape(trade.title)}\n\n"
        "Далі можна додати ноги:\n"
        f"<code>/pm_fill {html.escape(trade.trade_id)} --side BUY --outcome YES --price 0.47 --shares 638.3 --cost 300</code>\n"
        f"<code>/futures {html.escape(trade.trade_id)} --side SHORT --size-btc 0.0375 --entry 78000</code>"
    )


def render_pm_fill_command(text: str) -> str:
    parser = argparse.ArgumentParser(prog="/pm_fill")
    parser.add_argument("command")
    parser.add_argument("trade_id")
    parser.add_argument("--side", default="BUY")
    parser.add_argument("--outcome", required=True)
    parser.add_argument("--price", type=float, required=True)
    parser.add_argument("--shares", type=float, required=True)
    parser.add_argument("--cost", type=float)
    parser.add_argument("--pnl", type=float)
    try:
        args = parser.parse_args(shlex.split(text))
        trade = update_pm_leg(args.trade_id, args.side, args.outcome, args.price, args.shares, args.cost, args.pnl)
    except SystemExit:
        return (
            "Формат:\n"
            "<code>/pm_fill trade_id --side BUY --outcome YES --price 0.47 --shares 638.3 --cost 300 --pnl 42.5</code>"
        )
    except Exception as exc:
        return f"⚠️ <b>Не вдалося оновити Polymarket ногу</b>\n\n{html.escape(str(exc))}"

    return "✅ <b>Polymarket ногу оновлено</b>\n\n" + render_trade_line(trade)


def render_futures_command(text: str) -> str:
    parser = argparse.ArgumentParser(prog="/futures")
    parser.add_argument("command")
    parser.add_argument("trade_id")
    parser.add_argument("--side", choices=["LONG", "SHORT", "long", "short"], required=True)
    parser.add_argument("--size-btc", type=float, required=True)
    parser.add_argument("--entry", type=float, required=True)
    parser.add_argument("--exit", type=float)
    parser.add_argument("--pnl", type=float)
    try:
        args = parser.parse_args(shlex.split(text))
        trade = update_futures_leg(args.trade_id, args.side, args.size_btc, args.entry, args.exit, args.pnl)
    except SystemExit:
        return (
            "Формат:\n"
            "<code>/futures trade_id --side SHORT --size-btc 0.0375 --entry 78000 --exit 70000</code>"
        )
    except Exception as exc:
        return f"⚠️ <b>Не вдалося оновити futures ногу</b>\n\n{html.escape(str(exc))}"

    return "✅ <b>Futures ногу оновлено</b>\n\n" + render_trade_line(trade)


def render_clear_futures_command(text: str) -> str:
    parser = argparse.ArgumentParser(prog="/clear_futures")
    parser.add_argument("command")
    parser.add_argument("trade_id")
    try:
        args = parser.parse_args(shlex.split(text))
        trade = clear_futures_leg(args.trade_id)
    except SystemExit:
        return "Формат:\n<code>/clear_futures trade_id</code>"
    except Exception as exc:
        return f"⚠️ <b>Не вдалося видалити futures ногу</b>\n\n{html.escape(str(exc))}"

    return (
        "✅ <b>Futures-ногу видалено</b>\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "Тепер можеш внести правильну futures-угоду заново.\n\n"
        + render_trade_line(trade)
    )


def render_trade_line(trade: Any) -> str:
    payload = trade.payload or {}
    lines = [
        f"• Trade ID: <code>{html.escape(trade.trade_id)}</code>",
        f"• Ринок: {html.escape(trade.title)}",
    ]
    if payload.get("pm_price") is not None:
        lines.append(
            f"• PM: {html.escape(str(payload.get('pm_side', 'BUY')))} {html.escape(str(payload.get('pm_outcome', '')))} "
            f"{float(payload.get('pm_shares', 0.0)):.2f} @ {float(payload.get('pm_price', 0.0)):.3f} "
            f"| cost {money(float(payload.get('pm_cost', 0.0)))}"
        )
    if payload.get("futures_entry_price") is not None:
        futures = (
            f"• Futures: {html.escape(str(payload.get('futures_side', '')))} "
            f"{float(payload.get('futures_size_btc', 0.0)):.6f} BTC @ {money(float(payload.get('futures_entry_price', 0.0)))}"
        )
        if payload.get("futures_exit_price") is not None:
            futures += f" → {money(float(payload.get('futures_exit_price', 0.0)))}"
        if payload.get("futures_pnl") is not None:
            futures += f" | PnL {money(float(payload.get('futures_pnl', 0.0)))}"
        lines.append(futures)
    return "\n".join(lines)


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

    if data.startswith("pmpos:"):
        try:
            response = handle_polymarket_position_callback(data)
        except Exception as exc:
            self.answer_callback(callback_id, f"Помилка: {exc}")
            return
        self.answer_callback(callback_id, "Записано в журнал")
        self.send_report(chat_id, response)
        return

    if data.startswith("clearfut:"):
        try:
            response = handle_clear_futures_callback(data)
        except Exception as exc:
            self.answer_callback(callback_id, f"Помилка: {exc}")
            return
        self.answer_callback(callback_id, "Futures видалено")
        self.send_report(chat_id, response)
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
                "payout_multiple": opportunity.costs.payout_multiple,
                "net_no_win_flat": opportunity.costs.net_no_win_flat,
                "net_touch_with_hedge_tp": opportunity.costs.net_touch_with_hedge_tp,
                "net_no_win_after_hedge_sl": opportunity.costs.net_no_win_after_hedge_sl,
                "net_touch_after_hedge_sl_loss": opportunity.costs.net_touch_after_hedge_sl_loss,
                "touch_break_even_price": opportunity.costs.touch_break_even_price,
                "no_win_after_sl_break_even_price": opportunity.costs.no_win_after_sl_break_even_price,
                "no_exit_break_even_price": opportunity.costs.no_exit_break_even_price,
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
