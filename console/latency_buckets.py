"""Mergeable millisecond upper bounds for expired monitor samples."""
import math
from collections import Counter

_STEP = math.log1p(0.02)


def bucket(ms):
    if ms <= 0:
        return "0"
    if ms <= 1:
        return "1"
    return str(math.ceil(math.exp(math.ceil(math.log(ms) / _STEP) * _STEP)))


def quantile(histogram, p):
    count = sum(histogram.values())
    if not count:
        return None
    threshold = max(1, math.ceil(p * count))
    seen = 0
    for upper, frequency in sorted((float(k), v) for k, v in histogram.items()):
        seen += frequency
        if seen >= threshold:
            return upper


def merged(first, second):
    counts = Counter(first)
    counts.update(second)
    return dict(counts)
