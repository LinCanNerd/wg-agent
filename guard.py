"""Request budget + block cooldown, persisted in SQLite so restarts never reset it.

Every page load on wg-gesucht.de goes through RateGuard:
  - hourly and daily caps on page loads (background polling only)
  - escalating cooldowns after a block / captcha / 429 (15 min -> 1 h -> 3 h -> 12 h)
  - strikes decay after 24 h without problems
Sending a message you approved is 'priority': it ignores the caps (it's 2 page loads),
but it still waits out an active block cooldown.
"""

import json
import time


class BudgetExceeded(Exception):
    pass


class CoolingDown(Exception):
    pass


class RateGuard:
    def __init__(self, conn, cfg: dict):
        self.c = conn
        self.per_hour = int(cfg.get("max_pages_per_hour", 40))
        self.per_day = int(cfg.get("max_pages_per_day", 600))
        self.cooldowns = [m * 60 for m in cfg.get("block_cooldown_minutes", [15, 60, 180, 720])]
        self.c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
        self.c.commit()
        s = self._get("guard", {})
        self.hits: list[float] = s.get("hits", [])
        self.blocked_until: float = s.get("blocked_until", 0)
        self.strikes: int = s.get("strikes", 0)
        self.last_strike: float = s.get("last_strike", 0)

    # ---- persistence ----
    def _get(self, k, default):
        r = self.c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def _save(self):
        v = json.dumps(
            {
                "hits": self.hits,
                "blocked_until": self.blocked_until,
                "strikes": self.strikes,
                "last_strike": self.last_strike,
            }
        )
        self.c.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('guard', ?)", (v,))
        self.c.commit()

    # ---- accounting ----
    def _prune(self):
        now = time.time()
        self.hits = [t for t in self.hits if now - t < 86400]
        if self.strikes and now - self.last_strike > 86400:
            self.strikes = 0

    def used(self):
        self._prune()
        now = time.time()
        return sum(now - t < 3600 for t in self.hits), len(self.hits)

    def cooling_left(self):
        return max(0, int(self.blocked_until - time.time()))

    def remaining(self):
        h, d = self.used()
        return min(self.per_hour - h, self.per_day - d)

    def acquire(self, priority=False):
        """Call before every page load. Raises instead of loading if not allowed."""
        if self.cooling_left():
            raise CoolingDown(f"cooling down {self.cooling_left() // 60} min")
        if not priority and self.remaining() <= 0:
            raise BudgetExceeded("page budget used up")
        self.hits.append(time.time())
        self._save()

    def strike(self) -> int:
        """Record a block. Returns cooldown seconds."""
        self._prune()
        self.strikes += 1
        self.last_strike = time.time()
        wait = self.cooldowns[min(self.strikes - 1, len(self.cooldowns) - 1)]
        self.blocked_until = time.time() + wait
        self._save()
        return wait

    def clear(self):
        self.blocked_until = 0
        self._save()

    def status(self):
        h, d = self.used()
        s = f"pages: {h}/{self.per_hour} this hour, {d}/{self.per_day} today"
        if self.strikes:
            s += f", strikes: {self.strikes}"
        if self.cooling_left():
            s += f", cooling down {self.cooling_left() // 60} min"
        return s
