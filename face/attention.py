"""Attention watcher — idle-triggered presence checks (roadmap step 5).

**Redesigned 2026-07** (the always-on camera was uncomfortable): the camera is
NOT held open. While you're actively using the PC (keyboard/mouse), nothing
runs. Only after the session goes *idle* does the watcher periodically wake the
camera for a SINGLE snapshot, check whether a face is present, then release the
camera again.

    active (keys/mouse)   → camera off, PRESENT
    idle ≥ idle_after     → snapshot every `interval`s (camera opens, 1 frame, closes)
        snapshot: face    → PRESENT (you're reading/watching), keep checking
        snapshot: no face → DIM (warn) + a quick confirming snapshot
        `misses_to_lock` consecutive empty snapshots → LOCK (loginctl lock-session)

Any keyboard/mouse activity drops idle below the threshold, which cancels a dim
and stops the camera — that's handled by the watcher script, not here.

This module is the **pure logic** (snapshot decisions + miss counting) — no
OpenCV, no camera, no X11 — so it's unit-testable with a scripted snapshot
stream (`python3 attention.py --selftest`). watch_presence.py owns the side
effects: idle detection, opening/closing the camera, dimming, and locking.

Why count misses instead of wall-clock (as the old continuous model did):
snapshots are ~2 min apart and the screen sits below the camera (you look down
a lot), so a single missed frame must not lock you. We require `misses_to_lock`
*consecutive* empty snapshots; any detected face resets the counter.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class Phase(enum.Enum):
    PRESENT = "present"  # you're here (or recently seen) — nothing to do
    DIMMED = "dimmed"  # warned: screen dimmed, one empty snapshot so far
    LOCKED = "locked"  # locked the session; latched until reset()


@dataclass
class Config:
    idle_after: float = 60.0  # seconds of no input before checking starts
    interval: float = 120.0  # seconds between snapshots while present-but-idle
    confirm_after: float = 20.0  # seconds to the confirming snapshot after a miss
    misses_to_lock: int = 2  # consecutive empty snapshots before locking

    def __post_init__(self):
        if self.idle_after <= 0 or self.interval <= 0 or self.confirm_after <= 0:
            raise ValueError("idle_after, interval, confirm_after must be > 0")
        if self.misses_to_lock < 1:
            raise ValueError("misses_to_lock must be >= 1")


class SnapshotPresence:
    """Turns a stream of *snapshot* results into PRESENT/DIMMED/LOCKED.

    Feed it `record(face_found)` once per snapshot; it returns the current
    [`Phase`]. Ask `next_delay(phase)` for how long the caller should wait
    before the next snapshot (short after a miss, long while present). The
    caller owns the clock and the camera — this object only counts.
    """

    def __init__(self, config: Optional[Config] = None):
        self.cfg = config or Config()
        self._misses = 0
        self._locked = False

    def record(self, face_found: bool) -> Phase:
        # Latch: once locked, stay locked until the session unlocks and the
        # watcher restarts us via reset(). Prevents re-firing the lock.
        if self._locked:
            return Phase.LOCKED

        if face_found:
            self._misses = 0
            return Phase.PRESENT

        self._misses += 1
        if self._misses >= self.cfg.misses_to_lock:
            self._locked = True
            return Phase.LOCKED
        return Phase.DIMMED

    def next_delay(self, phase: Phase) -> float:
        """Seconds to wait before the next snapshot. After a miss we confirm
        quickly (so a real departure locks promptly); otherwise we idle the
        camera for the full interval."""
        if phase is Phase.DIMMED:
            return self.cfg.confirm_after
        return self.cfg.interval

    @property
    def locked(self) -> bool:
        return self._locked

    def reset(self) -> None:
        """Call when the user is active again, or after the session unlocks,
        to resume checking from a clean slate."""
        self._misses = 0
        self._locked = False


# ── self-test ────────────────────────────────────────────────────────────────

def _selftest() -> int:
    cfg = Config(idle_after=60, interval=120, confirm_after=20, misses_to_lock=2)
    failures = 0

    def check(name, cond):
        nonlocal failures
        if not cond:
            failures += 1
            print(f"FAIL: {name}")

    # 1. A face on every snapshot stays PRESENT.
    p = SnapshotPresence(cfg)
    for _ in range(10):
        ph = p.record(True)
    check("faces keep present", ph is Phase.PRESENT)

    # 2. One empty snapshot dims (warn); the next empty one locks.
    p = SnapshotPresence(cfg)
    check("first miss dims", p.record(False) is Phase.DIMMED)
    check("second miss locks", p.record(False) is Phase.LOCKED)

    # 3. A face between misses resets the counter — no lock on a look-down blip.
    p = SnapshotPresence(cfg)
    check("miss dims", p.record(False) is Phase.DIMMED)
    check("face returns → present", p.record(True) is Phase.PRESENT)
    check("next miss only dims again", p.record(False) is Phase.DIMMED)
    check("not locked after reset by face", not p.locked)

    # 4. Cadence: confirm quickly after a miss, idle long while present.
    p = SnapshotPresence(cfg)
    check("present → full interval", p.next_delay(Phase.PRESENT) == 120)
    check("dimmed → quick confirm", p.next_delay(Phase.DIMMED) == 20)

    # 5. Lock latches until reset().
    p = SnapshotPresence(cfg)
    p.record(False)
    p.record(False)  # locked now
    check("stays locked even if face returns", p.record(True) is Phase.LOCKED)
    p.reset()
    check("reset resumes presence", p.record(True) is Phase.PRESENT)

    # 6. misses_to_lock=1 locks on the very first empty snapshot.
    p1 = SnapshotPresence(Config(misses_to_lock=1))
    check("misses_to_lock=1 locks immediately", p1.record(False) is Phase.LOCKED)

    # 7. Config guards.
    for bad in (dict(idle_after=0), dict(interval=-1), dict(confirm_after=0),
                dict(misses_to_lock=0)):
        try:
            Config(**bad)
            check(f"bad config rejected {bad}", False)
        except ValueError:
            check(f"bad config rejected {bad}", True)

    if failures == 0:
        print("attention self-test: all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("usage: python3 attention.py --selftest")
