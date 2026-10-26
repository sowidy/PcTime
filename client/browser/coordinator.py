"""Desktop-authoritative grants, bounded to the 10-minute replay window."""
from collections import deque

from .protocol import BROWSERS


class BrowserCoordinator:
    def __init__(self):
        self.grants = deque(maxlen=4096)
        self.covered = deque(maxlen=4096)
        self.owner = None
        self.since = None
        self.last_observed = 0

    def observe(self, state, peers, now):
        active = state.get("app_name") if not state.get("is_idle") and not state.get("is_paused") else None
        candidates = [p for p in peers if p.get("focused") and BROWSERS[p["browser"]] == active]
        # At most one profile owns the foreground at any instant.
        owner = max(candidates, key=lambda p: p.get("focus_at", 0))["profile"] if candidates else None
        if now - self.last_observed > 3 and self.owner:
            self._end(min(now, self.last_observed + 1))
        if owner != self.owner:
            self._end(now)
            self.owner, self.since = owner, now if owner else None
        self.last_observed = now
        while self.grants and self.grants[0][2] < now - 600:
            self.grants.popleft()
        while self.covered and self.covered[0][1] < now - 600:
            self.covered.popleft()
        return owner

    def _end(self, now):
        if self.owner is not None and self.since is not None and now > self.since:
            self.grants.append((self.owner, self.since, now))
        self.owner, self.since = None, None

    def intersections(self, profile, start, end):
        spans = list(self.grants)
        if self.owner is not None:
            spans.append((self.owner, self.since, self.last_observed))
        result = []
        for owner, a, b in spans:
            if owner != profile or max(a, start) >= min(b, end):
                continue
            parts = [(max(a, start), min(b, end))]
            for c, d in self.covered:
                parts = [(x, y) for p, q in parts for x, y in
                         ((p, min(q, c)), (max(p, d), q)) if x < y]
            result.extend(parts)
        return result

    def committed(self, intervals):
        self.covered.extend(intervals)
