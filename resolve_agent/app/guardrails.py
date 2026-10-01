import re
import time
from collections import defaultdict, deque

CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def redact(text: str) -> str:
    """Strip card-like numbers before anything is stored or sent to the model."""
    return CARD.sub("[card number removed]", text)


class RateLimiter:
    def __init__(self, per_minute: int):
        self.n, self.hits = per_minute, defaultdict(deque)

    def allow(self, key: str) -> bool:
        now, q = time.time(), self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.n:
            return False
        q.append(now)
        return True
