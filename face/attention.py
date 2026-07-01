"""Attention watcher — auto-lock when you walk away (roadmap step 5).

While the session is unlocked, a *cheap presence* check ("is *a* face there?",
not "is it *you*?") runs continuously. When presence is lost:

    ... present ...  ── gone 3s ──►  DIM (warn, reversible)  ── gone 10s ──►  LOCK

Coming back at any point before LOCK cancels the dim and returns to PRESENT.

Like liveness.py / matcher.py this is **pure logic** — no OpenCV, no camera, no
`loginctl` — so the timing is unit-testable with a scripted presence stream
(`python3 attention.py --selftest`). The watcher script owns the side effects:
dimming the screen (a translucent overlay on X11) and calling
`loginctl lock-session` on LOCK. The daemon does the rest — it reacts to the
resulting logind `Lock` signal by wiping the unlock cache, so this module never
touches the cache directly.

Design choices for "the screen is below the camera, I look down a lot":
  - **Low sensitivity by debounce, not by threshold.** Any frame with a face
    resets the absence clock (`last_seen`), so intermittent misses while you're
    looking down keep you "present". You have to be *continuously* undetected
    for the full timeout to trip. Brief detection blips can't lock you.
  - Timers are measured from the last time a face was actually seen.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class Phase(enum.Enum):
    PRESENT = "present"  # you're here (or recently seen) — nothing to do
    DIMMED = "dimmed"  # warned: screen dimmed, still reversible
    LOCKED = "locked"  # locked the session; latched until reset()


@dataclass
class Config:
    dim_after: float = 3.0  # seconds of absence before dimming
    lock_after: float = 10.0  # seconds of absence before locking

    def __post_init__(self):
        if not (0 < self.dim_after < self.lock_after):
            raise ValueError("need 0 < dim_after < lock_after")


class AttentionWatcher:
    """Turns a stream of presence observations into PRESENT/DIMMED/LOCKED.

    Feed it `update(face_found, t)` per frame; it returns the current [`Phase`].
    Time is injected (each call takes a timestamp) so tests are deterministic and
    the caller sets the frame cadence.
    """

    def __init__(self, config: Optional[Config] = None):
        self.cfg = config or Config()
        self._last_seen: Optional[float] = None
        self._locked = False

    def update(self, face_found: bool, t: float) -> Phase:
        # Latch: once locked, stay locked until the session unlocks and the
        # watcher restarts us via reset(). Prevents re-firing the lock.
        if self._locked:
            return Phase.LOCKED

        # First observation seeds the clock as "seen now" — we never lock someone
        # out in the first few seconds just because detection hasn't warmed up.
        if self._last_seen is None:
            self._last_seen = t

        if face_found:
            self._last_seen = t
            return Phase.PRESENT

        absent = t - self._last_seen
        if absent >= self.cfg.lock_after:
            self._locked = True
            return Phase.LOCKED
        if absent >= self.cfg.dim_after:
            return Phase.DIMMED
        return Phase.PRESENT

    def reset(self) -> None:
        """Call after the session has re-unlocked, to resume watching."""
        self._last_seen = None
        self._locked = False


# ── self-test ────────────────────────────────────────────────────────────────

def _selftest() -> int:
    cfg = Config(dim_after=3.0, lock_after=10.0)
    failures = 0

    def check(name, cond):
        nonlocal failures
        if not cond:
            failures += 1
            print(f"FAIL: {name}")

    # 1. Continuous presence stays PRESENT.
    w = AttentionWatcher(cfg)
    for i in range(200):
        p = w.update(True, i * 0.1)
    check("continuous presence stays present", p is Phase.PRESENT)

    # 2. Absence dims at 3s, locks at 10s.
    w = AttentionWatcher(cfg)
    w.update(True, 0.0)  # seen at t=0
    check("still present at 2.9s absent", w.update(False, 2.9) is Phase.PRESENT)
    check("dimmed at 3.0s absent", w.update(False, 3.0) is Phase.DIMMED)
    check("dimmed at 9.9s absent", w.update(False, 9.9) is Phase.DIMMED)
    check("locked at 10.0s absent", w.update(False, 10.0) is Phase.LOCKED)

    # 3. Returning during the dim window cancels back to PRESENT (undim).
    w = AttentionWatcher(cfg)
    w.update(True, 0.0)
    check("dimmed at 4s", w.update(False, 4.0) is Phase.DIMMED)
    check("return cancels dim", w.update(True, 4.5) is Phase.PRESENT)
    check("no lock after return", w.update(False, 6.0) is Phase.PRESENT)  # clock reset at 4.5

    # 4. Look-down debounce: a single detected frame resets the absence clock.
    w = AttentionWatcher(cfg)
    w.update(True, 0.0)
    w.update(False, 2.0)
    w.update(True, 2.5)  # brief glance up
    for tt in [4.0, 6.0, 8.0, 11.0]:  # absent again, but only ~from 2.5
        p = w.update(False, tt)
    # at t=11.0, absent since 2.5 = 8.5s → dimmed, NOT locked
    check("blip resets clock, not locked at 11s", p is Phase.DIMMED)

    # 5. Lock latches until reset().
    w = AttentionWatcher(cfg)
    w.update(True, 0.0)
    w.update(False, 10.0)
    check("stays locked even if face returns", w.update(True, 10.5) is Phase.LOCKED)
    w.reset()
    check("reset resumes presence", w.update(True, 11.0) is Phase.PRESENT)

    # 6. Config guard.
    try:
        Config(dim_after=10, lock_after=3)
        check("bad config rejected", False)
    except ValueError:
        check("bad config rejected", True)

    if failures == 0:
        print("attention self-test: all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("usage: python3 attention.py --selftest")
