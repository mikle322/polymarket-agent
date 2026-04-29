import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket_hedge_bot.formatting import positive_result_probability
from polymarket_hedge_bot.scout import Opportunity


DATA_DIR = Path("data")
OPPORTUNITY_HISTORY_PATH = DATA_DIR / "opportunity_history.jsonl"
_HISTORY_LOCK = threading.RLock()


def record_opportunity_history(
    opportunities: list[Opportunity],
    matched_keys: set[str],
    diagnostics: dict[str, Any],
    limit: int = 100,
) -> int:
    if not opportunities:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    records = [
        opportunity_to_record(opportunity, now, opportunity_key in matched_keys, diagnostics)
        for opportunity, opportunity_key in (
            (item, f"{item.candidate.slug}:{item.decision}") for item in opportunities[:limit]
        )
    ]

    with _HISTORY_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with OPPORTUNITY_HISTORY_PATH.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(records)


def opportunity_to_record(
    opportunity: Opportunity,
    recorded_at: str,
    matched_alert: bool,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    candidate = opportunity.candidate
    return {
        "recorded_at": recorded_at,
        "matched_alert": matched_alert,
        "slug": candidate.slug,
        "question": candidate.question,
        "market_type": candidate.market_type,
        "deadline": candidate.deadline.isoformat(),
        "strike": candidate.strike,
        "direction": candidate.direction,
        "btc_price": candidate.btc_price,
        "iv": candidate.iv,
        "no_price": candidate.no_price,
        "stake": candidate.stake,
        "decision": opportunity.decision,
        "reason": opportunity.reason,
        "score": opportunity.score,
        "positive_probability": positive_result_probability(opportunity.edge, opportunity.costs),
        "edge": asdict(opportunity.edge),
        "quality": asdict(opportunity.quality),
        "hedge": asdict(opportunity.hedge),
        "costs": asdict(opportunity.costs),
        "liquidity": asdict(opportunity.liquidity),
        "pm_shares": opportunity.pm_shares,
        "worst_case_after_sl": opportunity.worst_case_after_sl,
        "risk_ratio": opportunity.risk_ratio,
        "scanner_context": {
            "candidates_loaded": diagnostics.get("candidates_loaded"),
            "candidates_after_prefilter": diagnostics.get("candidates_after_prefilter"),
            "opportunities_analyzed": diagnostics.get("opportunities_analyzed"),
            "matched_alert_filters": diagnostics.get("matched_alert_filters"),
        },
    }
