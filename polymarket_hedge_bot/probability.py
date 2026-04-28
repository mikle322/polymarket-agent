import math
from datetime import datetime, timezone


def years_until(deadline: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    seconds = (deadline - now).total_seconds()
    return max(0.0, seconds / (365.0 * 24.0 * 60.0 * 60.0))


def normal_cdf(value: float) -> float:
    return 0.5 * math.erfc(-value / math.sqrt(2.0))


def touch_probability(
    spot: float,
    strike: float,
    annualized_volatility: float,
    years_to_expiry: float,
    direction: str = "up",
) -> float:
    if spot <= 0 or strike <= 0 or annualized_volatility <= 0 or years_to_expiry <= 0:
        return 0.0

    if direction == "up":
        if spot >= strike:
            return 1.0
        x = math.log(strike / spot) / (annualized_volatility * math.sqrt(years_to_expiry))
    elif direction == "down":
        if spot <= strike:
            return 1.0
        x = math.log(spot / strike) / (annualized_volatility * math.sqrt(years_to_expiry))
    else:
        raise ValueError("direction must be 'up' or 'down'")

    return min(1.0, max(0.0, 2.0 * normal_cdf(-x)))
