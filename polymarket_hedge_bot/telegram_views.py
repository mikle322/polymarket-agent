import html

from polymarket_hedge_bot.costs import CostResult
from polymarket_hedge_bot.edge import EdgeResult
from polymarket_hedge_bot.formatting import (
    action_label,
    entry_requirement,
    main_problem,
    money,
    pct,
    positive_result_probability,
    ua_reason,
)
from polymarket_hedge_bot.hedge import HedgeResult
from polymarket_hedge_bot.liquidity import LiquidityCheck
from polymarket_hedge_bot.quality import QualityResult
from polymarket_hedge_bot.scout import Opportunity


def tag(text: str) -> str:
    return html.escape(str(text))


def code(text: str) -> str:
    return f"<code>{tag(text)}</code>"


def bold(text: str) -> str:
    return f"<b>{tag(text)}</b>"


def divider() -> str:
    return "━━━━━━━━━━━━━━━━"


def status_badge(decision: str) -> str:
    badges = {
        "ENTER": "🟢 ENTER",
        "WATCH": "🟡 WATCH",
        "SKIP": "🔴 SKIP",
    }
    return badges.get(decision, decision)


def beginner_summary(decision: str) -> str:
    summaries = {
        "ENTER": "Є умови для входу, але фінальне рішення тільки після live orderbook, funding та post-SL плану.",
        "WATCH": "Ідея потенційно цікава, але зараз умови ще не достатньо чисті.",
        "SKIP": "Краще пропустити: угода не дає достатньо якісного входу за поточними фільтрами.",
    }
    return summaries.get(decision, "Потрібна додаткова перевірка.")


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
    positive_probability = positive_result_probability(edge, costs)
    return "\n".join(
        [
            f"🧭 <b>Висновок:</b> {code(status_badge(decision))}",
            tag(beginner_summary(decision)),
            divider(),
            "",
            f"📌 <b>Ринок:</b> {tag(market)}",
            f"🎯 <b>Ймовірність позитивного результату:</b> {pct(positive_probability)}",
            f"⭐ <b>Якість угоди:</b> {tag(quality.label)}",
            f"⚠️ <b>Головна проблема:</b> {tag(main_problem(reason, edge, worst_case_after_sl, liquidity))}",
            f"✅ <b>Що треба для входу:</b> {tag(entry_requirement(decision, reason, liquidity))}",
            "",
            "🛠 <b>План дії</b>",
            f"• PM: купити {code('NO')} лімітним ордером на <b>{money(stake)}</b>",
            f"• Futures: {code(hedge.side)} <b>{hedge.size_btc:.6f} BTC</b>",
            "• Біржа hedge: <b>Binance Futures</b>",
            f"• Плече: <b>{hedge.leverage:.1f}x isolated</b>",
            f"• Margin: <b>{money(hedge.isolated_margin)}</b>",
            f"• TP / SL: <b>{money(hedge.take_profit)}</b> / <b>{money(hedge.stop_loss)}</b>",
            "",
            "📊 <b>Risk / Edge</b>",
            f"• Touch: <b>{pct(edge.fair_touch)}</b> | Fair NO: <b>{pct(edge.fair_no)}</b>",
            f"• NO price: <b>{edge.no_price:.3f}</b> | Edge: <b>{pct(edge.true_edge)}</b>",
            f"• Коефіцієнт ставки: <b>{costs.payout_multiple:.2f}x</b>",
            f"• Net upside: <b>{money(quality.net_upside)}</b> | Reward/Risk: <b>{quality.reward_risk:.2f}</b>",
            f"• Worst-case: <b>{money(worst_case_after_sl)}</b>",
            f"• SL loss: <b>{money(hedge.expected_sl_loss)}</b>",
            "",
            "💰 <b>Чисті сценарії</b>",
            f"• Флет / no-touch: <b>{money(costs.net_no_win_flat)}</b>",
            f"• touch + TP: <b>{money(costs.net_touch_with_hedge_tp)}</b>",
            f"• SL + NO wins: <b>{money(costs.net_no_win_after_hedge_sl)}</b>",
            f"• SL + touch: <b>{money(costs.net_touch_after_hedge_sl_loss)}</b>",
            f"• Funding: <b>{money(costs.funding_cost)}</b>",
            "",
            "⚖️ <b>Беззбиток</b>",
            f"• Touch hedge break-even: <b>{money(costs.touch_break_even_price)}</b>",
            f"• NO після hedge SL break-even: <b>{costs.no_win_after_sl_break_even_price:.3f}</b>",
            f"• Достроковий вихід NO break-even: <b>{costs.no_exit_break_even_price:.3f}</b>",
            "",
            "🔎 <b>Службово</b>",
            f"• Причина: {tag(ua_reason(reason))}",
            f"• Ліквідність: {tag(ua_reason(liquidity.reason))}",
        ]
    )


def render_scout_cards(opportunities: list[Opportunity], top: int) -> str:
    shown = opportunities[:top]
    parts = [
        "🔍 <b>Сканер угод</b>",
        f"Проскановано: <b>{len(opportunities)}</b> | Показую: <b>{len(shown)}</b>",
        "Нижче найкращі кандидати за поточними фільтрами.",
    ]

    for index, opportunity in enumerate(shown, start=1):
        candidate = opportunity.candidate
        edge = opportunity.edge
        hedge = opportunity.hedge
        costs = opportunity.costs
        positive_probability = positive_result_probability(edge, costs)
        parts.extend(
            [
                "",
                divider(),
                f"<b>{index}. {tag(status_badge(opportunity.decision))}</b> | score <b>{opportunity.score:.1f}</b>",
                f"{code(candidate.slug)}",
                "",
                f"🧭 <b>Висновок:</b> {tag(action_label(opportunity.decision)).capitalize()}.",
                tag(beginner_summary(opportunity.decision)),
                f"🎯 <b>Ймовірність позитивного результату:</b> {pct(positive_probability)}",
                f"⭐ <b>Якість угоди:</b> {tag(opportunity.quality.label)}",
                f"⚠️ <b>Проблема:</b> {tag(main_problem(opportunity.reason, edge, opportunity.worst_case_after_sl, opportunity.liquidity))}",
                f"✅ <b>Для входу:</b> {tag(entry_requirement(opportunity.decision, opportunity.reason, opportunity.liquidity))}",
                "",
                "🛠 <b>Дія</b>",
                f"• PM: купити {code('NO')} лімітним ордером на <b>{money(candidate.stake)}</b> -> <b>{opportunity.pm_shares:.2f}</b> shares",
                f"• Futures: {code(hedge.side)} <b>{hedge.size_btc:.6f} BTC</b> | <b>{hedge.leverage:.1f}x isolated</b>",
                "• Біржа hedge: <b>Binance Futures</b>",
                f"• Margin: <b>{money(hedge.isolated_margin)}</b>",
                f"• TP / SL: <b>{money(hedge.take_profit)}</b> / <b>{money(hedge.stop_loss)}</b>",
                "",
                "📊 <b>Ключові цифри</b>",
                f"• Touch: <b>{pct(edge.fair_touch)}</b> | Fair NO: <b>{pct(edge.fair_no)}</b> | Edge: <b>{pct(edge.true_edge)}</b>",
                f"• NO price: <b>{edge.no_price:.3f}</b> | Коеф: <b>{costs.payout_multiple:.2f}x</b>",
                f"• Net upside: <b>{money(opportunity.quality.net_upside)}</b> | Reward/Risk: <b>{opportunity.quality.reward_risk:.2f}</b>",
                f"• Worst-case: <b>{money(opportunity.worst_case_after_sl)}</b>",
                f"• Флет/no-touch: <b>{money(costs.net_no_win_flat)}</b>",
                f"• Touch+TP: <b>{money(costs.net_touch_with_hedge_tp)}</b> | SL+NO: <b>{money(costs.net_no_win_after_hedge_sl)}</b>",
                f"• SL+touch worst: <b>{money(costs.net_touch_after_hedge_sl_loss)}</b>",
                f"• Беззбиток: hedge <b>{money(costs.touch_break_even_price)}</b> | NO exit <b>{costs.no_exit_break_even_price:.3f}</b>",
                f"• Ліквідність: {tag(ua_reason(opportunity.liquidity.reason))}",
            ]
        )

    return "\n".join(parts)
