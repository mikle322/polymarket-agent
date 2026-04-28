import argparse
from urllib.error import HTTPError, URLError

from polymarket_hedge_bot.connectors.binance_futures import BinanceFuturesConnector
from polymarket_hedge_bot.connectors.okx_futures import OkxFuturesConnector
from polymarket_hedge_bot.formatting import money
from polymarket_hedge_bot.utils import safe_print


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="binance-market")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--okx-inst-id", default="BTC-USDT-SWAP")
    parser.add_argument("--depth", type=int, default=10)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return print_binance(args)
    except HTTPError as exc:
        if exc.code != 451:
            raise
        safe_print("Binance Futures API повернув HTTP 451. Перемикаюсь на OKX public data.")
        return print_okx(args)
    except URLError as exc:
        safe_print(f"Binance недоступний: {exc}. Перемикаюсь на OKX public data.")
        return print_okx(args)


def print_binance(args: argparse.Namespace) -> int:
    connector = BinanceFuturesConnector()
    ticker = connector.ticker_price(args.symbol)
    premium = connector.premium_index(args.symbol)
    book = connector.orderbook(args.symbol, args.depth)

    best_bid = book.bids[0].price if book.bids else 0.0
    best_ask = book.asks[0].price if book.asks else 0.0

    safe_print(
        "\n".join(
            [
                "Source: Binance USD-M Futures",
                f"Symbol: {ticker.symbol}",
                f"Last price: {money(ticker.price)}",
                f"Mark price: {money(premium.mark_price)}",
                f"Index price: {money(premium.index_price)}",
                f"Funding rate: {premium.last_funding_rate * 100:.4f}%",
                f"Next funding time: {premium.next_funding_time}",
                f"Best bid/ask: {money(best_bid)} / {money(best_ask)}",
            ]
        )
    )
    return 0


def print_okx(args: argparse.Namespace) -> int:
    connector = OkxFuturesConnector()
    ticker = connector.ticker(args.okx_inst_id)
    funding = connector.funding_rate(args.okx_inst_id)
    book = connector.orderbook(args.okx_inst_id, args.depth)

    best_bid = book.bids[0].price if book.bids else ticker.bid
    best_ask = book.asks[0].price if book.asks else ticker.ask

    safe_print(
        "\n".join(
            [
                "Source: OKX SWAP",
                f"Instrument: {ticker.inst_id}",
                f"Last price: {money(ticker.last)}",
                f"Funding rate: {funding.funding_rate * 100:.4f}%",
                f"Next funding rate: {funding.next_funding_rate * 100:.4f}%" if funding.next_funding_rate is not None else "Next funding rate: n/a",
                f"Funding time: {funding.funding_time}",
                f"Next funding time: {funding.next_funding_time}",
                f"Best bid/ask: {money(best_bid)} / {money(best_ask)}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
