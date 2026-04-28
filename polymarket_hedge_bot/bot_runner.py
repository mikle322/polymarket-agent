import argparse
import os
import threading
import time

from polymarket_hedge_bot import scanner
from polymarket_hedge_bot.telegram_bot import TelegramBot
from polymarket_hedge_bot.utils import load_dotenv, safe_print


def build_parser() -> argparse.ArgumentParser:
    parser = scanner.build_parser()
    parser.prog = "polymarket-bot-runner"
    parser.description = "Run Telegram bot and 24/7 scanner in one process."
    parser.add_argument("--token", help="Telegram bot token. Defaults to TELEGRAM_BOT_TOKEN")
    parser.add_argument("--allowed-chat-id", help="Optional chat allowlist. Defaults to TELEGRAM_ALLOWED_CHAT_ID")
    parser.add_argument("--telegram-timeout", type=int, default=30)
    parser.add_argument(
        "--no-telegram-polling",
        action="store_true",
        help="Do not listen for Telegram commands, but keep scanner alerts enabled.",
    )
    parser.add_argument("--telegram-only", action="store_true", help="Run only Telegram command listener, without scanner.")
    return parser


def build_bot(args: argparse.Namespace) -> tuple[TelegramBot | None, str | None]:
    if args.dry_run:
        return None, None

    token = args.token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = args.allowed_chat_id or os.environ.get("TELEGRAM_ALLOWED_CHAT_ID")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing. Add it to .env or pass --token.")
    if not chat_id:
        raise SystemExit("TELEGRAM_ALLOWED_CHAT_ID is missing. Add it to .env or pass --allowed-chat-id.")
    return TelegramBot(token=token, allowed_chat_id=chat_id, timeout=args.telegram_timeout), chat_id


def start_telegram_thread(bot: TelegramBot, stop_event: threading.Event) -> threading.Thread:
    def run() -> None:
        try:
            bot.run()
        except Exception as exc:
            safe_print(f"Telegram bot stopped with error: {exc}")
            stop_event.set()

    thread = threading.Thread(target=run, name="telegram-bot", daemon=True)
    thread.start()
    return thread


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    load_dotenv(args.env_file)

    bot, chat_id = build_bot(args)
    stop_event = threading.Event()

    if args.telegram_only:
        if bot is None:
            safe_print("Telegram dry-run mode: nothing to poll.")
            return 0
        bot.run()
        return 0

    if bot is not None and not args.no_telegram_polling:
        start_telegram_thread(bot, stop_event)
        safe_print("Telegram command listener started")
        time.sleep(0.2)

    config = scanner.config_from_args(args)
    return scanner.run_scanner_loop(
        config,
        bot=bot,
        chat_id=chat_id,
        dry_run=args.dry_run,
        once=args.once,
        stop_event=stop_event,
    )


if __name__ == "__main__":
    raise SystemExit(main())
