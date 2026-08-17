"""Long-only portfolio optimisers.

All four produce weights on `{w : sum(w) = 1, 0 <= w_i <= cap}` and all four are
**deterministic** - same input, identical output - because the E2E asserts on the result.
No randomness, no multi-start, fixed iteration counts.

Which objective uses mu is the design decision worth remembering (PORTFOLIO_ANALYTICS.md
§2): `min_variance`, `risk_parity` and `equal_weight` depend only on Sigma, and one of them
is the default. `max_sharpe` needs mu, and the mu available here is the simulator's damped,
capped drift - an optimiser fed that produces confidently wrong tilts, so it is offered and
labelled but never the default.
"""

from __future__ import annotations

import math

from .risk import matvec, quadratic_form

MAX_ITERATIONS = 500
TOLERANCE = 1e-12
PROJECTION_STEPS = 40


class Infeasible(ValueError):
    """The constraint set is empty. Raised with the arithmetic spelled out, because
    'optimisation failed' tells a user nothing they can act on."""


def project(vector: list[float], cap: float) -> list[float]:
    """Euclidean projection onto {w : sum(w) = 1, 0 <= w_i <= cap}.

    Solved by bisection on the single dual variable theta, where w_i = clip(v_i - theta, 0,
    cap): the sum is monotonically decreasing in theta, so bisection cannot get stuck. The
    sort-based closed form is faster and considerably easier to get subtly wrong.
    """
    n = len(vector)
    if n == 0:
        return []
    if cap * n < 1.0 - 1e-12:
        raise Infeasible(
            f"max_weight {cap:.4f} is below 1/{n} = {1 / n:.4f}: {n} names capped at "
            f"{cap:.0%} can hold at most {cap * n:.0%} of the portfolio"
        )

    low = min(vector) - cap - 1.0          # every w_i at the cap -> sum >= 1
    high = max(vector)                     # every w_i at 0       -> sum = 0
    # The bracket starts a few units wide, so 40 halvings put theta within ~1e-12 - far
    # tighter than weights rendered to two decimals need. This is the hot loop: every
    # backtracking candidate of every solve is projected, thousands of times per frontier,
    # so the 200 steps this started with were most of the cost and all of them waste.
    for _ in range(PROJECTION_STEPS):
        theta = (low + high) / 2
        total = sum(min(max(value - theta, 0.0), cap) for value in vector)
        if total > 1.0:
            low = theta
        else:
            high = theta
    theta = (low + high) / 2
    weights = [min(max(value - theta, 0.0), cap) for value in vector]

    # Bisection leaves a residue around 1e-16; renormalise so downstream code can trust
    # sum(w) == 1 rather than carrying an epsilon everywhere.
    total = sum(weights)
    return [w / total for w in weights] if total > 0 else [1.0 / n] * n


def _descend(objective, gradient, start: list[float], cap: float,
             maximize: bool = False, step_hint: float = 1.0) -> tuple[list[float], float]:
    """Projected gradient with backtracking, shared by min-variance and max-Sharpe.

    Backtracking rather than a fixed 1/L step: max-Sharpe's gradient has no useful Lipschitz
    bound (it divides by sigma_p^3), so a step chosen for min-variance either crawls or
    diverges there. Every accepted step strictly improves the objective, which makes the
    loop monotone and its termination unconditional.
    """
    sign = 1.0 if maximize else -1.0
    weights = project(start, cap)
    value = objective(weights)

    # The accepted step size is carried between iterations AND returned to the caller, so a
    # sweep of related problems can hand it to the next solve. Rediscovering it from 1.0
    # costs ~30 backtracking halvings on the first iteration of every solve; with a median
    # of 4 iterations per solve, that one line was most of the frontier's runtime.
    for _ in range(MAX_ITERATIONS):
        direction = [sign * g for g in gradient(weights)]
        step = step_hint * 2.0
        improved = False
        while step > 1e-14:
            candidate = project([w + step * d for w, d in zip(weights, direction)], cap)
            candidate_value = objective(candidate)
            if (candidate_value > value + TOLERANCE) if maximize else (candidate_value < value - TOLERANCE):
                weights, value, improved = candidate, candidate_value, True
                step_hint = step
                break
            step /= 2
        if not improved:
            break
    return weights, step_hint


def equal_weight(n: int, cap: float) -> list[float]:
    if n == 0:
        return []
    return project([1.0 / n] * n, cap)


def min_variance(cov: list[list[float]], cap: float) -> list[float]:
    n = len(cov)
    if n == 0:
        return []
    return _descend(
        objective=lambda w: quadratic_form(cov, w),
        gradient=lambda w: [2.0 * value for value in matvec(cov, w)],
        start=[1.0 / n] * n,
        cap=cap,
    )[0]


def max_sharpe(mu: list[float], cov: list[list[float]], risk_free: float,
               cap: float) -> list[float]:
    """Tangency portfolio under long-only + cap.

    Started from the min-variance solution rather than equal weights: the objective is
    quasi-concave on the simplex, so one start suffices, and this one is both closer and
    deterministic.
    """
    n = len(mu)
    if n == 0:
        return []

    def sharpe(w: list[float]) -> float:
        vol = math.sqrt(quadratic_form(cov, w))
        if vol <= 0:
            return float("-inf")
        return (sum(a * b for a, b in zip(w, mu)) - risk_free) / vol

    def gradient(w: list[float]) -> list[float]:
        vol = math.sqrt(quadratic_form(cov, w))
        if vol <= 0:
            return list(mu)
        excess = sum(a * b for a, b in zip(w, mu)) - risk_free
        sigma_w = matvec(cov, w)
        return [mu[i] / vol - excess * sigma_w[i] / vol ** 3 for i in range(n)]

    return _descend(sharpe, gradient, start=min_variance(cov, cap), cap=cap,
                    maximize=True)[0]


def risk_parity(cov: list[list[float]], cap: float) -> list[float]:
    """Equal risk contribution: every name supplies 1/n of the portfolio's volatility.

    Standard multiplicative fixed point, w_i <- w_i * (target / RC_i)^0.5, with the cap
    projection applied INSIDE the loop rather than once at the end. Projecting afterwards
    would hand back a vector that satisfies the cap and is no longer risk parity, with
    nothing in the output saying so.
    """
    n = len(cov)
    if n == 0:
        return []
    weights = equal_weight(n, cap)

    for _ in range(MAX_ITERATIONS):
        vol = math.sqrt(quadratic_form(cov, weights))
        if vol <= 0:
            break
        sigma_w = matvec(cov, weights)
        target = vol / n
        updated = []
        for i in range(n):
            contribution = weights[i] * sigma_w[i] / vol
            if contribution <= 1e-15:
                updated.append(weights[i] + target)       # nudge a dead name back in
            else:
                updated.append(weights[i] * math.sqrt(target / contribution))
        updated = project(updated, cap)
        if max(abs(a - b) for a, b in zip(updated, weights)) < TOLERANCE:
            weights = updated
            break
        weights = updated

    return weights


def frontier_steps(mu: list[float], cov: list[list[float]], cap: float = 1.0,
                   points: int = 32):
    """Yield the sweep one solved point at a time, unfiltered and unsorted.

    A generator so the caller can hand control back to the event loop between solves. Each
    one is ~12ms of pure Python, and 48 of them back to back is half a second during which
    nothing else in the process runs - long enough to visibly stall the price stream.
    """
    n = len(mu)
    if n < 2:
        return

    weights = min_variance(cov, cap)
    hint = 1.0
    for step in range(points):
        exponent = 1.5 - 3.0 * step / (points - 1)          # 1e1.5 down to 1e-1.5
        lam = 10.0 ** exponent
        weights, hint = _descend(
            objective=lambda w: (sum(a * b for a, b in zip(w, mu))
                                 - lam * quadratic_form(cov, w)),
            gradient=lambda w, l=lam: [
                mu[i] - 2.0 * l * value for i, value in enumerate(matvec(cov, w))
            ],
            start=weights,
            cap=cap,
            maximize=True,
            step_hint=hint,
        )
        yield (math.sqrt(quadratic_form(cov, weights)),
               sum(a * b for a, b in zip(weights, mu)))


def efficient_only(curve: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Keep the efficient branch. The sweep produces it in order, but rounding can leave a
    point another beats on BOTH axes; plotted, that reads as a kink in a curve whose whole
    meaning is "nothing above and to the left of this is achievable"."""
    efficient: list[tuple[float, float]] = []
    for volatility, expected in sorted(curve):
        if efficient and expected <= efficient[-1][1] + 1e-9:
            continue
        efficient.append((volatility, expected))
    return efficient


def frontier(mu: list[float], cov: list[list[float]], cap: float = 1.0,
             points: int = 32) -> list[tuple[float, float]]:
    """The long-only efficient frontier, as (volatility, expected return) pairs.

    Traced by sweeping risk aversion: for each lambda, maximise `mu'w - lambda*w'Sigma w`
    over the capped simplex. Large lambda lands on the minimum-variance portfolio, small
    lambda on the highest-return one, and everything between is on the frontier by
    construction. This reuses the solver the objectives already use - there is no separate
    piece of optimisation maths to keep correct.

    Each solve is **warm-started from the previous one**. Consecutive lambdas differ
    slightly, so their solutions do too, and a warm start converges in a handful of
    backtracking steps instead of the hundreds a cold start needs.

    Synchronous, for tests and scripts. The request path drives `frontier_steps` directly so
    it can yield to the event loop between solves.
    """
    return efficient_only(list(frontier_steps(mu, cov, cap, points)))


def frontier_gap(volatility: float, expected_return: float,
                 curve: list[tuple[float, float]]) -> dict:
    """How far inside the frontier a portfolio sits, in both directions.

    Two numbers, because "how far from optimal am I" has two honest readings: the risk you
    could drop while earning the same, and the return you could add while risking the same.
    Linear interpolation between adjacent frontier points is plenty at 32 of them.
    """
    if len(curve) < 2:
        return {}

    def interpolate(target: float, source_index: int, wanted_index: int) -> float | None:
        pairs = sorted(curve, key=lambda point: point[source_index])
        if target <= pairs[0][source_index]:
            return pairs[0][wanted_index]
        if target >= pairs[-1][source_index]:
            return pairs[-1][wanted_index]
        for left, right in zip(pairs, pairs[1:]):
            if left[source_index] <= target <= right[source_index]:
                span = right[source_index] - left[source_index]
                if span <= 0:
                    return left[wanted_index]
                ratio = (target - left[source_index]) / span
                return left[wanted_index] + ratio * (right[wanted_index] - left[wanted_index])
        return None

    best_volatility = interpolate(expected_return, 1, 0)
    best_return = interpolate(volatility, 0, 1)
    if best_volatility is None or best_return is None:
        return {}

    return {
        # Clamped at zero: a portfolio cannot beat the frontier, so a negative gap is
        # interpolation noise, and reporting "-0.2% of avoidable risk" reads as a bug.
        "volatility_at_same_return": round(best_volatility, 6),
        "avoidable_volatility": round(max(volatility - best_volatility, 0.0), 6),
        "return_at_same_volatility": round(best_return, 6),
        "forgone_return": round(max(best_return - expected_return, 0.0), 6),
    }


OBJECTIVES = ("min_variance", "risk_parity", "max_sharpe", "equal_weight")


def solve(objective: str, mu: list[float], cov: list[list[float]], risk_free: float,
          cap: float) -> list[float]:
    if objective == "min_variance":
        return min_variance(cov, cap)
    if objective == "risk_parity":
        return risk_parity(cov, cap)
    if objective == "max_sharpe":
        return max_sharpe(mu, cov, risk_free, cap)
    if objective == "equal_weight":
        return equal_weight(len(cov), cap)
    raise ValueError(f"unknown objective {objective!r}; expected one of {OBJECTIVES}")


def apply_floor(weights: list[float], floor: float, cap: float) -> list[float]:
    """Zero anything below `floor` and renormalise, so the plan does not carry a 0.3%
    position whose trade would be filtered out as dust anyway.

    Skipped when it would zero everything - a floor above 1/n on an equal-weight solution
    would otherwise empty the portfolio.

    The renormalisation runs over the SURVIVING entries only. Re-projecting the full vector
    looks equivalent and is not: `project` shifts every element by the same theta, and with
    the kept weights summing to less than 1 that theta is negative - which lifts the just-
    zeroed entries straight back off zero.
    """
    if floor <= 0:
        return weights
    kept = [i for i, w in enumerate(weights) if w >= floor]
    if not kept:
        return weights
    survivors = project([weights[i] for i in kept], cap)
    result = [0.0] * len(weights)
    for slot, index in enumerate(kept):
        result[index] = survivors[slot]
    return result
