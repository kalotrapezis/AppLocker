"""Liveness challenge — proves a *live* person, not a photo, is at the camera.

This is a requirement, not a nicety: face-at-login must never accept a static
frame (a printed photo, a phone screen). The user chose "liveness required", and
on a plain RGB webcam this challenge–response is the software bar we can raise.

Design: this module is **pure logic**, deliberately free of OpenCV and the
camera. It consumes a stream of abstract per-frame observations —

    FrameObservation(face_found: bool, eyes_open: Optional[bool], yaw: Optional[float])

— where `yaw` is a signed head-turn signal (negative = turned to the subject's
left, positive = right; units are whatever the engine emits, with a matching
threshold). How those signals are derived from pixels is the engine's problem
(see engine.py). Keeping the state machine hardware-free means the anti-spoof
logic is fully unit-testable with scripted frame sequences — see the tests at
the bottom, runnable with plain `python3 liveness.py --selftest`.

The challenge is a short randomized sequence — always a blink plus a random head
turn direction — each step bounded by a timeout, with the face required to stay
present throughout. Randomizing the turn direction stops a replayed video of one
fixed gesture from passing every time.
"""

from __future__ import annotations

import enum
import random
from dataclasses import dataclass
from typing import List, Optional


class Action(enum.Enum):
    BLINK = "blink"
    TURN_LEFT = "turn_left"
    TURN_RIGHT = "turn_right"

    def human(self) -> str:
        return {
            Action.BLINK: "Blink",
            Action.TURN_LEFT: "Turn your head left",
            Action.TURN_RIGHT: "Turn your head right",
        }[self]


class Status(enum.Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


@dataclass
class FrameObservation:
    """One frame's worth of signals from the face engine."""

    face_found: bool
    eyes_open: Optional[bool] = None  # None = couldn't tell this frame
    yaw: Optional[float] = None  # <0 left, >0 right, ~0 centre; None = unknown


@dataclass
class Config:
    per_step_timeout: float = 8.0  # seconds allowed to complete each step
    yaw_threshold: float = 0.30  # |yaw| past this counts as a deliberate turn
    # Seconds the face may vanish before we fail. Generous, because Haar loses
    # the face mid-turn on a plain webcam — a real removal still exceeds this.
    max_face_gap: float = 2.5


def random_challenge(rng: Optional[random.Random] = None) -> List[Action]:
    """A blink plus one randomly-chosen turn direction, in random order."""
    rng = rng or random.Random()
    turn = rng.choice([Action.TURN_LEFT, Action.TURN_RIGHT])
    steps = [Action.BLINK, turn]
    rng.shuffle(steps)
    return steps


def turn_challenge(rng: Optional[random.Random] = None) -> List[Action]:
    """Both head turns, in random order — no blink. The login-tier challenge:
    blink detection is unreliable on plain webcams (confirmed on real hardware),
    while yaw from YuNet landmarks is robust. A photo can't turn either way; the
    random order resists a pre-recorded clip."""
    rng = rng or random.Random()
    steps = [Action.TURN_LEFT, Action.TURN_RIGHT]
    rng.shuffle(steps)
    return steps


class LivenessVerifier:
    """Drives a challenge to completion over a stream of `update()` calls.

    Time is *injected* (each `update` takes a timestamp), never read from the
    clock, so tests are deterministic and the caller controls the frame cadence.
    """

    def __init__(self, steps: List[Action], config: Optional[Config] = None):
        if not steps:
            raise ValueError("a liveness challenge needs at least one step")
        self.steps = steps
        self.cfg = config or Config()
        self.status = Status.PENDING
        self.reason: Optional[str] = None
        self._i = 0
        self._step_started_at: Optional[float] = None
        self._last_face_at: Optional[float] = None
        # blink sub-state: we require open -> closed -> open.
        self._blink_open_seen = False
        self._blink_closed_seen = False

    @property
    def current(self) -> Optional[Action]:
        if self.status is Status.PENDING and self._i < len(self.steps):
            return self.steps[self._i]
        return None

    def update(self, obs: FrameObservation, t: float) -> Status:
        if self.status is not Status.PENDING:
            return self.status

        # First frame of the (current) step anchors its clock.
        if self._step_started_at is None:
            self._step_started_at = t
            self._last_face_at = t

        # Face continuity: a photo swapped in, or the subject leaving, shows up
        # as a sustained loss of a detected face.
        if obs.face_found:
            self._last_face_at = t
        elif t - (self._last_face_at or t) > self.cfg.max_face_gap:
            return self._fail("face lost")

        # Per-step deadline.
        if t - self._step_started_at > self.cfg.per_step_timeout:
            return self._fail(f"timed out on: {self.current.value}")

        step = self.steps[self._i]
        if step is Action.BLINK:
            self._eval_blink(obs)
        elif step is Action.TURN_LEFT:
            if obs.yaw is not None and obs.yaw <= -self.cfg.yaw_threshold:
                self._advance()
        elif step is Action.TURN_RIGHT:
            if obs.yaw is not None and obs.yaw >= self.cfg.yaw_threshold:
                self._advance()

        return self.status

    def _eval_blink(self, obs: FrameObservation) -> None:
        if obs.eyes_open is True:
            if self._blink_open_seen and self._blink_closed_seen:
                self._advance()  # open -> closed -> open completed
            else:
                self._blink_open_seen = True
        elif obs.eyes_open is False:
            # Only counts as the "closed" phase once we've established a baseline
            # of open eyes, so starting mid-blink can't shortcut it.
            if self._blink_open_seen:
                self._blink_closed_seen = True
        # eyes_open is None: no information this frame, ignore.

    def _advance(self) -> None:
        self._i += 1
        self._step_started_at = None  # re-anchored on the next update
        self._blink_open_seen = False
        self._blink_closed_seen = False
        if self._i >= len(self.steps):
            self.status = Status.PASSED

    def _fail(self, reason: str) -> Status:
        self.status = Status.FAILED
        self.reason = reason
        return self.status


# ── self-test (no pytest needed; runs anywhere) ──────────────────────────────

def _selftest() -> int:
    cfg = Config(per_step_timeout=5.0, yaw_threshold=0.3, max_face_gap=1.0)
    failures = 0

    def check(name: str, cond: bool) -> None:
        nonlocal failures
        if not cond:
            failures += 1
            print(f"FAIL: {name}")

    # Helper: feed frames at 0.1s cadence.
    def run(steps, frames):
        v = LivenessVerifier(steps, cfg)
        t = 0.0
        for obs in frames:
            v.update(obs, t)
            t += 0.1
        return v

    face = lambda **k: FrameObservation(face_found=True, **k)

    # 1. Blink then turn-left completes.
    frames = (
        [face(eyes_open=True)] * 3
        + [face(eyes_open=False)] * 2
        + [face(eyes_open=True)] * 2
        + [face(yaw=-0.5)] * 2
    )
    v = run([Action.BLINK, Action.TURN_LEFT], frames)
    check("blink+left passes", v.status is Status.PASSED)

    # 2. A static photo (eyes always open, no turn) never passes. 60 frames at
    #    0.1s = 5.9s, past the 5s per-step timeout on the blink step.
    v = run([Action.BLINK, Action.TURN_LEFT], [face(eyes_open=True, yaw=0.0)] * 60)
    check("static photo fails (timeout)", v.status is Status.FAILED)

    # 3. Wrong turn direction doesn't satisfy the step (blink completes, then the
    #    turn step times out because every turn is the wrong way).
    frames = [face(eyes_open=True)] * 2 + [face(eyes_open=False)] + [face(eyes_open=True)]
    frames += [face(yaw=0.5)] * 60  # asked left, turns right, long enough to time out
    v = run([Action.BLINK, Action.TURN_LEFT], frames)
    check("wrong turn direction fails", v.status is Status.FAILED)

    # 4. Losing the face mid-challenge fails.
    frames = [face(eyes_open=True)] * 2 + [FrameObservation(face_found=False)] * 20
    v = run([Action.BLINK], frames)
    check("face lost fails", v.status is Status.FAILED and v.reason == "face lost")

    # 5. Blink can't be shortcut by starting closed.
    frames = [face(eyes_open=False)] * 5 + [face(eyes_open=True)] * 2
    v = run([Action.BLINK], frames)
    check("closed-first does not count as blink", v.status is Status.PENDING)

    # 6. Turn-right challenge completes on a rightward turn.
    frames = [face(yaw=0.0)] + [face(yaw=0.6)] * 2
    v = run([Action.TURN_RIGHT], frames)
    check("turn-right passes", v.status is Status.PASSED)

    # 7. random_challenge is always a blink + one turn.
    for seed in range(20):
        ch = random_challenge(random.Random(seed))
        check(
            f"random_challenge seed={seed}",
            len(ch) == 2
            and Action.BLINK in ch
            and any(a in ch for a in (Action.TURN_LEFT, Action.TURN_RIGHT)),
        )

    if failures == 0:
        print("liveness self-test: all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("usage: python3 liveness.py --selftest")
