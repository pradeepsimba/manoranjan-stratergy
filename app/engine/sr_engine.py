from __future__ import annotations
from typing import List

from app.models import Candle
from app.state import SRLevels


def detect(candles: List[Candle]) -> SRLevels:
    if not candles or len(candles) < 5:
        return SRLevels([], [])

    supports:    List[float] = []
    resistances: List[float] = []
    for i in range(2, len(candles) - 2):
        p2 = candles[i - 2].close; p1 = candles[i - 1].close
        c  = candles[i].close
        n1 = candles[i + 1].close; n2 = candles[i + 2].close
        if c < p1 and c < p2 and c < n1 and c < n2: supports.append(c)
        if c > p1 and c > p2 and c > n1 and c > n2: resistances.append(c)

    return SRLevels(
        supports=_cluster(supports,    0.25)[-3:],
        resistances=_cluster(resistances, 0.25)[-3:],
    )


def _cluster(levels: List[float], threshold: float) -> List[float]:
    if not levels:
        return []
    sorted_lvls = sorted(levels)
    clusters:  List[float] = []
    group:     List[float] = [sorted_lvls[0]]
    for i in range(1, len(sorted_lvls)):
        prev = sorted_lvls[i - 1]
        if prev > 0 and abs(sorted_lvls[i] - prev) / prev < threshold / 100.0:
            group.append(sorted_lvls[i])
        else:
            clusters.append(sum(group) / len(group))
            group = [sorted_lvls[i]]
    clusters.append(sum(group) / len(group))
    return clusters
