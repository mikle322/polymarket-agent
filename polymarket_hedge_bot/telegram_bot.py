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
from polymarket_hedge_bot.probability import touch_probability, years_until
from polymarket_hedge_bot.quality import calculate_quality
from polymarket_hedge_bot.scout import load_candidates, scout_candidates
from polymarket_hedge_bot.telegram_views import render_analyze_card, render_scout_cards
from polymarket_hedge_bot.utils import load_dotenv, safe_print


HELP_TEXT = """Polymarket Hedge Bot

Команди:
/analyze <args>
/scout <args>
/monitor <args>
/pm_liquidity <args>
/journal
/close <trade_id> --pnl <amount> --note "optional note"
/ping
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
            body = chunk if is_html else f"<pre>{html.escape(chunk)}</pre>"
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
    if text in {"/start", "/help"}:
        return TelegramResponse(HELP_TEXT)
    if text == "/ping":
        return TelegramResponse("pong")
    if text == "/journal":
        return TelegramResponse(journal_summary())
    if text.startswith("/close"):
        return TelegramResponse(handle_close_command(text))

    try:
        argv = telegram_text_to_cli_args(text)
    except ValueError as exc:
        return TelegramResponse(f"Command error: {exc}\n\n{HELP_TEXT}")

    if not argv:
        return TelegramResponse(HELP_TEXT)

    if argv[0] == "analyze":
        return run_analyze_with_buttons(argv)
    if argv[0] == "scout":
        return run_scout_with_buttons(argv)
    return TelegramResponse(run_cli(argv))


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
