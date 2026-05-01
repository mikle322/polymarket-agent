from polymarket_hedge_bot.costs import CostResult
from polymarket_hedge_bot.edge import EdgeResult
from polymarket_hedge_bot.hedge import HedgeResult
from polymarket_hedge_bot.liquidity import LiquidityCheck
from polymarket_hedge_bot.monitor import MonitorResult
from polymarket_hedge_bot.quality import QualityResult
from polymarket_hedge_bot.scout import Opportunity


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def money(value: float) -> str:
    if value < 0:
        return f"-${abs(value):,.2f}"
    return f"${value:,.2f}"


def ua_reason(reason: str) -> str:
    translations = {
        "true edge is below watch threshold": "true edge нижче порогу WATCH",
        "edge exists, but worst-case after SL exceeds risk limit": "edge є, але worst-case після SL перевищує risk limit",
        "edge and risk are inside configured limits": "edge і risk входять у задані ліміти",
        "edge is positive but below enter threshold": "edge позитивний, але нижче порогу ENTER",
        "basic liquidity checks passed": "базова перевірка ліквідності пройдена",
        "Polymarket spread is wider than 8c": "spread на Polymarket ширший за 8c",
        "Polymarket liquidity is below 3x stake": "ліквідність Polymarket нижча за 3x stake",
        "enough ask liquidity for intended stake": "достатньо ask-ліквідності для заданого stake",
        "not enough ask liquidity to fill intended stake": "недостатньо ask-ліквідності, щоб набрати заданий stake",
        "VWAP is above max acceptable NO price": "VWAP вище максимально прийнятної ціни NO",
        "orderbook slippage is too high for intended stake": "slippage в orderbook завеликий для заданого stake",
        "orderbook has no asks": "в orderbook немає asks",
        "no usable ask liquidity": "немає придатної ask-ліквідності",
        "live orderbook requested, but candidate has no no_token_id": "увімкнено live orderbook, але кандидат не має no_token_id",
        "do not enter": "не входити",
        "reduce stake, reduce coverage, or plan partial PM exit after SL": "зменшити stake, coverage або запланувати partial PM exit після SL",
        "after SL: re-hedge once, partial exit, full exit, or freeze + alert": "після SL: re-hedge, partial exit, full exit або freeze + alert",
        "wait for better NO price or higher distance to strike": "чекати кращу ціну NO або більшу дистанцію до strike",
        "sell enough PM exposure so broken-hedge worst-case returns inside max loss": "продати стільки PM exposure, щоб broken-hedge worst-case повернувся в max loss",
        "realized futures loss already reached or exceeded max loss": "зафіксований futures loss уже досяг або перевищив max loss",
        "worst-case is still inside max loss": "worst-case ще входить у max loss",
    }
    if reason.startswith("trade quality filter failed:"):
        return "фільтр якості угоди не пройдений: " + reason.split(":", 1)[1].strip()
    return translations.get(reason, reason)


def positive_result_probability(edge: EdgeResult, costs: CostResult) -> float:
    probability = 0.0
    if max(costs.net_no_win_flat, costs.net_no_win_after_hedge_sl) > 0:
        probability += edge.fair_no
    if costs.net_touch_with_hedge_tp > 0:
        probability += edge.fair_touch
    return min(1.0, max(0.0, probability))


def recommendation_text(
    decision: str,
    edge: EdgeResult,
    costs: CostResult,
    worst_case_after_sl: float,
    liquidity: LiquidityCheck,
    quality: QualityResult | None = None,
) -> str:
    positive_probability = positive_result_probability(edge, costs)

    if not liquidity.ok:
        return "Пропустити: ліквідність не дозволяє нормально набрати позицію."
    if decision == "ENTER":
        return "Можна розглядати ENTER: edge достатній, risk у межах ліміту. Перед входом перевірити live orderbook, funding, fee та post-SL план."
    if decision == "WATCH":
        if edge.true_edge <= 0:
            return "Не входити зараз: edge слабкий або від'ємний, краще чекати кращу ціну NO."
        if worst_case_after_sl > 0 and costs.net_touch_after_hedge_sl_loss < 0:
            return "WATCH: ідея цікава, але risk завеликий. Потрібно зменшити stake, coverage або мати partial exit після SL."
        return "WATCH: є потенціал, але сигнал ще не достатньо чистий для ENTER."
    if decision == "SKIP":
        if positive_probability < 0.5:
            return "SKIP: ймовірність позитивного результату замала для такого ризику."
        return "SKIP: формально шанс є, але edge/risk/liquidity не проходять фільтри."
    return "Потрібна додаткова перевірка."


def action_label(decision: str) -> str:
    labels = {
        "ENTER": "можна розглядати вхід",
        "WATCH": "спостерігати, але не входити зараз",
        "SKIP": "пропустити",
    }
    return labels.get(decision, decision)


def main_problem(
    reason: str,
    edge: EdgeResult,
    worst_case_after_sl: float,
    liquidity: LiquidityCheck,
) -> str:
    if not liquidity.ok:
        return ua_reason(liquidity.reason)
    if "worst-case after SL exceeds risk limit" in reason:
        return f"worst-case після SL + PM touch = {money(worst_case_after_sl)}, risk завеликий."
    if edge.true_edge < 0:
        return f"true edge від'ємний: {pct(edge.true_edge)}."
    if "below watch threshold" in reason:
        return f"true edge занадто слабкий: {pct(edge.true_edge)}."
    if "below enter threshold" in reason:
        return f"true edge ще не дотягує до ENTER: {pct(edge.true_edge)}."
    return ua_reason(reason)


def entry_requirement(decision: str, reason: str, liquidity: LiquidityCheck) -> str:
    if not liquidity.ok:
        return "Потрібна краща ліквідність або менший stake."
    if decision == "ENTER":
        return "Перевірити live orderbook, funding, fee та post-SL план."
    if "worst-case after SL exceeds risk limit" in reason:
        return "Зменшити stake/coverage або мати чіткий partial exit після SL."
    if decision == "WATCH":
        return "Чекати кращу ціну NO, більший edge або нижчий risk."
    return "Немає умов для входу зараз."


def format_analyze_report(
    market: str,
    stake: float,
    decision: str,
    reason: str,
    edge: EdgeResult,
    hedge: HedgeResult,
    costs: CostResult,
    quality: QualityResult | None,
    worst_case_after_sl: float,
    post_sl_action: str,
    liquidity: LiquidityCheck,
) -> str:
    lines = [
        f"Ринок: {market}",
        f"Рішення: {decision}",
        "",
        "ВИСНОВОК:",
        f"{action_label(decision).capitalize()}.",
        f"Ймовірність позитивного результату: {pct(positive_result_probability(edge, costs))}",
        f"Головна проблема: {main_problem(reason, edge, worst_case_after_sl, liquidity)}",
        f"Що треба для входу: {entry_requirement(decision, reason, liquidity)}",
        "",
        "ДІЯ:",
        f"PM: купити NO на {money(stake)}",
        f"Futures: {hedge.side} {hedge.size_btc:.6f} BTC | {hedge.leverage:.1f}x isolated | margin {money(hedge.isolated_margin)}",
        f"TP: {money(hedge.take_profit)} | SL: {money(hedge.stop_loss)}",
        "",
        "ДЕТАЛІ:",
        f"Причина: {ua_reason(reason)}",
        f"Рекомендація: {recommendation_text(decision, edge, costs, worst_case_after_sl, liquidity)}",
        f"Touch probability: {pct(edge.fair_touch)}",
        f"Fair NO: {pct(edge.fair_no)}",
        f"NO entry price: {edge.no_price:.3f}",
        f"Коефіцієнт ставки: {costs.payout_multiple:.2f}x",
        f"True edge: {pct(edge.true_edge)}",
        f"Futures notional: {money(hedge.notional)}",
        f"Очікуваний hedge TP profit: {money(hedge.expected_tp_profit)}",
        f"Очікуваний hedge SL loss: {money(hedge.expected_sl_loss)}",
        f"Витрати до TP: {money(costs.total_cost_to_tp)}",
        f"Витрати до SL: {money(costs.total_cost_to_sl)}",
        f"Витрати якщо флет/no-touch: {money(costs.total_cost_to_flat)}",
        f"Funding cost: {money(costs.funding_cost)}",
        f"Net якщо флет/no-touch: {money(costs.net_no_win_flat)}",
        f"Net якщо touch + hedge TP: {money(costs.net_touch_with_hedge_tp)}",
        f"Net якщо hedge SL, потім NO wins: {money(costs.net_no_win_after_hedge_sl)}",
        f"Net якщо hedge SL, потім touch: {money(costs.net_touch_after_hedge_sl_loss)}",
        f"Touch hedge break-even: {money(costs.touch_break_even_price)}",
        f"NO після hedge SL break-even: {costs.no_win_after_sl_break_even_price:.3f}",
        f"Достроковий NO exit break-even: {costs.no_exit_break_even_price:.3f}",
        f"Якість угоди: {quality.label if quality else 'n/a'}",
        f"Net upside: {money(quality.net_upside) if quality else 'n/a'}",
        f"Reward/Risk: {quality.reward_risk:.2f}" if quality else "Reward/Risk: n/a",
        f"Worst-case після SL + PM touch: {money(worst_case_after_sl)}",
        f"Post-SL план: {ua_reason(post_sl_action)}",
        f"Ліквідність: {ua_reason(liquidity.reason)}",
    ]
    return "\n".join(lines)


def format_scout_report(opportunities: list[Opportunity], top: int) -> str:
    lines = [
        f"Проскановано ринків: {len(opportunities)}",
        f"Показую top: {min(top, len(opportunities))}",
        "",
    ]

    for index, opportunity in enumerate(opportunities[:top], start=1):
        candidate = opportunity.candidate
        hedge = opportunity.hedge
        edge = opportunity.edge
        costs = opportunity.costs
        lines.extend(
            [
                f"{index}. {opportunity.decision} | score {opportunity.score:.1f} | {candidate.slug}",
                "",
                "ВИСНОВОК:",
                f"{action_label(opportunity.decision).capitalize()}.",
                f"Ймовірність позитивного результату: {pct(positive_result_probability(edge, costs))}",
                f"Головна проблема: {main_problem(opportunity.reason, edge, opportunity.worst_case_after_sl, opportunity.liquidity)}",
                f"Що треба для входу: {entry_requirement(opportunity.decision, opportunity.reason, opportunity.liquidity)}",
                "",
                "ДІЯ:",
                f"PM: купити NO на {money(candidate.stake)} -> {opportunity.pm_shares:.2f} shares",
                f"Futures: {hedge.side} {hedge.size_btc:.6f} BTC | {hedge.leverage:.1f}x isolated | margin {money(hedge.isolated_margin)}",
                f"TP: {money(hedge.take_profit)} | SL: {money(hedge.stop_loss)}",
                "",
                "ДЕТАЛІ:",
                f"Питання: {candidate.question}",
                f"Причина: {ua_reason(opportunity.reason)}",
                f"Рекомендація: {recommendation_text(opportunity.decision, edge, costs, opportunity.worst_case_after_sl, opportunity.liquidity)}",
                f"Touch: {pct(edge.fair_touch)} | Fair NO: {pct(edge.fair_no)} | NO: {edge.no_price:.3f} | Коеф: {costs.payout_multiple:.2f}x | Edge: {pct(edge.true_edge)}",
            ]
        )
        if opportunity.liquidity.vwap is not None:
            lines.append(
                "PM liquidity: "
                f"best ask {opportunity.liquidity.best_ask:.3f} | "
                f"VWAP {opportunity.liquidity.vwap:.3f} | "
                f"worst {opportunity.liquidity.worst_price:.3f} | "
                f"levels {opportunity.liquidity.levels_used}"
            )
        lines.extend(
            [
                f"Futures: notional {money(hedge.notional)} | SL loss {money(hedge.expected_sl_loss)}",
                f"Витрати: TP path {money(costs.total_cost_to_tp)} | SL path {money(costs.total_cost_to_sl)} | funding {money(costs.funding_cost)}",
                f"Net scenarios: flat/no-touch {money(costs.net_no_win_flat)} | touch+TP {money(costs.net_touch_with_hedge_tp)} | SL+NO wins {money(costs.net_no_win_after_hedge_sl)} | SL+touch {money(costs.net_touch_after_hedge_sl_loss)}",
                f"Break-even: hedge {money(costs.touch_break_even_price)} | NO after SL {costs.no_win_after_sl_break_even_price:.3f} | NO exit {costs.no_exit_break_even_price:.3f}",
                f"Якість: {opportunity.quality.label} | Net upside {money(opportunity.quality.net_upside)} | Reward/Risk {opportunity.quality.reward_risk:.2f}",
                f"Worst-case після SL + PM touch: {money(opportunity.worst_case_after_sl)}",
                f"Post-SL план: {ua_reason(opportunity.post_sl_action)}",
                f"Ліквідність: {ua_reason(opportunity.liquidity.reason)}",
                "",
            ]
        )

    return "\n".join(lines).rstrip()


def format_monitor_report(result: MonitorResult) -> str:
    lines = [
        f"Hedge status: {result.hedge_status}",
        f"Дія: {result.action}",
        f"Причина: {ua_reason(result.reason)}",
        "",
        f"Realized futures loss: {money(result.realized_futures_loss)}",
        f"Поточний PM profit: {money(result.current_pm_profit)}",
        f"Worst-case якщо тримати весь PM: {money(result.worst_case_hold_all)}",
        f"Дозволений залишковий PM cost: {money(result.allowed_remaining_pm_cost)}",
        "",
        f"Продати PM: {pct(result.sell_fraction)}",
        f"Залишити PM: {pct(result.keep_fraction)}",
        f"Продати shares: {result.sell_shares:.2f}",
        f"Залишити shares: {result.keep_shares:.2f}",
        f"Орієнтовно cash з продажу: {money(result.estimated_cash_from_sale)}",
        f"Worst-case після дії: {money(result.worst_case_after_action)}",
    ]
    return "\n".join(lines)


def format_liquidity_report(
    token_id: str,
    result: LiquidityCheck,
    market_slug: str | None = None,
    question: str | None = None,
    outcome: str | None = None,
    tick_size: float | None = None,
    min_order_size: float | None = None,
) -> str:
    lines: list[str] = []
    if market_slug is not None:
        lines.append(f"Ринок: {market_slug}")
    if question is not None:
        lines.append(f"Питання: {question}")
    if outcome is not None:
        lines.append(f"Outcome: {outcome}")
    lines.extend(
        [
            f"Token ID: {token_id}",
            f"Ліквідність OK: {'YES' if result.ok else 'NO'}",
            f"Причина: {ua_reason(result.reason)}",
            "",
            f"Запитаний stake: {money(result.requested_cost)}",
            f"Заповнений cost: {money(result.filled_cost)}",
            f"Отримані shares: {result.filled_shares:.2f}",
        ]
    )
    if result.best_ask is not None:
        lines.append(f"Best ask: {result.best_ask:.3f}")
    if result.vwap is not None:
        lines.append(f"VWAP: {result.vwap:.3f}")
    if result.worst_price is not None:
        lines.append(f"Worst filled price: {result.worst_price:.3f}")
    if result.slippage_from_best is not None:
        lines.append(f"Slippage від best ask: {result.slippage_from_best:.3f}")
    lines.append(f"Використано рівнів orderbook: {result.levels_used}")
    lines.append(f"Доступний ask-side cost: {money(result.available_cost)}")
    if tick_size is not None:
        lines.append(f"Tick size: {tick_size}")
    if min_order_size is not None:
        lines.append(f"Min order size: {min_order_size}")
    return "\n".join(lines)
