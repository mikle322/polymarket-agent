import argparse

from polymarket_hedge_bot.connectors.deribit import DeribitConnector
from polymarket_hedge_bot.utils import safe_print


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deribit-iv")
    parser.add_argument("--lookback-min", type=int, default=30)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    connector = DeribitConnector()
    vol = connector.btc_volatility_index(args.lookback_min)
    safe_print(
        "\n".join(
            [
                f"Source: {vol.source}",
                f"Currency: {vol.currency}",
                f"Annualized IV input: {vol.annualized_volatility:.4f}",
                f"Annualized IV percent: {vol.annualized_volatility * 100:.2f}%",
                f"Timestamp: {vol.timestamp}",
            ]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
