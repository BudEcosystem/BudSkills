"""Just enough statistics to stop the toolkit lying about small differences.

Two prompts run on the same cases, so every comparison here is *paired*. The
paired test is worth roughly a free doubling of the case count, and it is the
only honest way to read a 50-case suite.
"""

from __future__ import annotations

import math


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a pass rate. Behaves at 0% and 100%."""
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def mcnemar_exact(fixes: int, regressions: int) -> float:
    """Two-sided exact McNemar p-value.

    Only the *discordant* cases carry information: those the candidate fixed
    (`fixes`) and those it broke (`regressions`). Cases both prompts got right,
    or both got wrong, tell you nothing about which is better.
    """
    n = fixes + regressions
    if n == 0:
        return 1.0
    k = min(fixes, regressions)
    tail = sum(math.comb(n, i) for i in range(k + 1)) * (0.5 ** n)
    return min(1.0, 2 * tail)


def min_clean_fixes(alpha: float = 0.05) -> int:
    """How many fixes with zero regressions are needed to clear `alpha`.

    At the conventional 0.05 this is 6 - which is 12 percentage points on a
    50-case suite. Anything smaller is not a result, whatever the average says.
    """
    for b in range(1, 200):
        if mcnemar_exact(b, 0) <= alpha:
            return b
    return 200


def detectable_delta(total: int, alpha: float = 0.05) -> float:
    """Smallest pass-rate delta that could possibly be significant on `total` cases."""
    if total <= 0:
        return 1.0
    return min_clean_fixes(alpha) / total


def paired_counts(baseline: dict[str, bool], candidate: dict[str, bool]) -> dict:
    """Split the shared cases into the four McNemar cells."""
    shared = sorted(set(baseline) & set(candidate))
    both_pass = [c for c in shared if baseline[c] and candidate[c]]
    both_fail = [c for c in shared if not baseline[c] and not candidate[c]]
    fixed = [c for c in shared if not baseline[c] and candidate[c]]
    broken = [c for c in shared if baseline[c] and not candidate[c]]
    return {
        "shared": shared,
        "both_pass": both_pass,
        "both_fail": both_fail,
        "fixed": fixed,
        "broken": broken,
        "only_in_baseline": sorted(set(baseline) - set(candidate)),
        "only_in_candidate": sorted(set(candidate) - set(baseline)),
    }
