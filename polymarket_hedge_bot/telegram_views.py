import html

from polymarket_hedge_bot.costs import CostResult
from polymarket_hedge_bot.edge import EdgeResult
from polymarket_hedge_bot.formatting import action_label, entry_requirement, main_problem, money, pct, positive_result_probability, ua_reason
from polymarket_hedge_bot.hedge import HedgeResult
from polymarket_hedge_bot.liquidity import LiquidityCheck
from polymarket_hedge_bot.quality import QualityResult
from polymarket_hedge_bot.scout import Opportunity


def tag(text: str) -> str:
    return html.escape(text)


def code(text: str) -> str:
    return f"<code>{tag(text)}</code>"


def bold(text: str) -> str:
    return f"<b>{tag(text)}</b>"


def status_badge(decision: str) -> str:
    badges = {
        "ENTER": "🟢 ENTER",
        "WATCH": "🟡 WATCH",
        "SKIP": "🔴 SKIP",
    }
    return badges.get(decision, decision)


def beginner_summary(decision: str) -> str:
    summaries = {
        "ENTER": "Угоду можна розглядати, але тільки після перевірки live-ліквідності та готового плану після SL.",
        "WATCH": "Ідея потенційно цікава, але зараз ризик або умови ще не ідеальні.",
        "SKIP": "Краще пропустити. Умови не дають достатньо якісного входу.",
    }
    return summaries.get(decision, "Потрібна додаткова перевірка.")


def divider() -> str:
    return "━━━━━━━━━━━━━━━━"


def render_analyze_card(
    market: str,
    stake: float,
    decision: str,
    reason: str,
    edge: EdgeResult,
    hedge: HedgeResult,
    costs: CostResult,
    quality: QualityResult,
    worst_case_after_sl: float,
    liquidity: LiquidityCheck,
) -> str:
    return "\n".join(
        [
            f"{bold('🧭 Висновок')} {code(status_badge(decision))}",
            f"{tag(beginner_summary(decision))}",
            divider(),
            "",
            f"{bold('📌 Ринок')}: {tag(market)}",
            f"{bold('🎯 Ймовірність NO wins')}: {pct(edge.fair_no)}",
            f"{bold('⭐ Якість угоди')}: {tag(quality.label)}",
            f"{bold('⚠️ Головна проблема')}: {tag(main_problem(reason, edge, worst_case_after_sl, liquidity))}",
            f"{bold('✅ Що треба для входу')}: {tag(entry_requirement(decision, reason, liquidity))}",
            "",
            f"{bold('🛠 Дія')}",
            f"PM: купити {code('NO')} на {money(stake)}",
            f"Futures: {code(hedge.side)} {hedge.size_btc:.6f} BTC",
            f"Плече: {hedge.leverage:.1f}x isolated",
            f"Margin: {money(hedge.isolated_margin)}",
            f"TP: {money(hedge.take_profit)} | SL: {money(hedge.stop_loss)}",
            "",
            f"{bold('📊 Ризик і edge')}",
            f"Touch: {pct(edge.fair_touch)} | Fair NO: {pct(edge.fair_no)}",
            f"NO price: {edge.no_price:.3f} | Edge: {pct(edge.true_edge)}",
            f"Net upside: {money(quality.net_upside)} | Reward/Risk: {quality.reward_risk:.2f}",
            f"Worst-case: {money(worst_case_after_sl)}",
            f"SL loss: {money(hedge.expected_sl_loss)}",
            "",
            f"{bold('💰 Чисті сценарії')}",
            f"touch + TP: {money(costs.net_touch_with_hedge_tp)}",
            f"SL + NO wins: {money(costs.net_no_win_after_hedge_sl)}",
            f"SL + touch: {money(costs.net_touch_after_hedge_sl_loss)}",
            "",
            f"{bold('🔎 Службово')}",
            f"Причина: {tag(ua_reason(reason))}",
            f"Ліквідність: {tag(ua_reason(liquidity.reason))}",
        ]
    )


def render_scout_cards(opportunities: list[Opportunity], top: int) -> str:
    shown = opportunities[:top]
    parts = [
        f"{bold('🔍 Сканер угод')}",
        f"Проскановано: {len(opportunities)} | Показую: {len(shown)}",
        "Нижче — найкращі кандидати за поточними фільтрами.",
    ]

    for index, opportunity in enumerate(shown, start=1):
        candidate = opportunity.candidate
        edge = opportunity.edge
        hedge = opportunity.hedge
        costs = opportunity.costs
        parts.extend(
            [
                "",
                divider(),
                f"{bold(str(index) + '. ' + status_badge(opportunity.decision))} | score {opportunity.score:.1f}",
                f"{tag(candidate.slug)}",
                "",
                f"{bold('🧭 Висновок')}: {tag(action_label(opportunity.decision))}.",
                f"{tag(beginner_summary(opportunity.decision))}",
                f"{bold('🎯 Ймовірність NO wins')}: {pct(edge.fair_no)}",
                f"{bold('⭐ Якість угоди')}: {tag(opportunity.quality.label)}",
                f"{bold('⚠️ Проблема')}: {tag(main_problem(opportunity.reason, edge, opportunity.worst_case_after_sl, opportunity.liquidity))}",
                f"{bold('✅ Для входу')}: {tag(entry_requirement(opportunity.decision, opportunity.reason, opportunity.liquidity))}",
                "",
                f"{bold('🛠 Дія')}",
                f"PM: купити {code('NO')} на {money(candidate.stake)} -> {opportunity.pm_shares:.2f} shares",
                f"Futures: {code(hedge.side)} {hedge.size_btc:.6f} BTC | {hedge.leverage:.1f}x isolated",
                f"Margin: {money(hedge.isolated_margin)}",
                f"TP: {money(hedge.take_profit)} | SL: {money(hedge.stop_loss)}",
                "",
                f"{bold('📊 Ключові цифри')}",
                f"Touch: {pct(edge.fair_touch)} | Fair NO: {pct(edge.fair_no)} | Edge: {pct(edge.true_edge)}",
                f"Net upside: {money(opportunity.quality.net_upside)} | Reward/Risk: {opportunity.quality.reward_risk:.2f}",
                f"Worst-case: {money(opportunity.worst_case_after_sl)}",
                f"Net: TP {money(costs.net_touch_with_hedge_tp)} | NO wins {money(costs.net_no_win_after_hedge_sl)} | touch {money(costs.net_touch_after_hedge_sl_loss)}",
                f"Ліквідність: {tag(ua_reason(opportunity.liquidity.reason))}",
            ]
        )

    return "\n".join(parts)
