"""Face matching — "is this *me*?" — over enrolled embeddings.

The recognition tier answers a yes/no against one enrolled identity (the machine
owner). We model an enrolled face as a set of L2-normalised embedding vectors
(captured from several angles during enrollment) and match a probe frame by
cosine similarity to the nearest enrolled vector, above a threshold.

Like liveness.py this is **pure logic** — no OpenCV, no camera — so it unit-tests
with synthetic vectors (`python3 matcher.py --selftest`). The engine (engine.py)
produces the actual embeddings; the *metric*, *threshold* and *K-of-N* debounce
live here.

Threshold note: the right threshold depends on the embedding backend and is
calibrated at enrollment, so it's stored *in the enrollment file*, not hardcoded.
Whatever the backend, a single lucky frame must never unlock — recognition
requires K matching frames out of a sliding window of N (see MatchAccumulator),
mirroring the daemon-side "up to 3×" retry but at frame granularity.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

ENROLL_VERSION = 1


def l2_normalize(v: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0.0:
        return list(v)
    return [x / norm for x in v]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity in [-1, 1]. Assumes equal length; callers guarantee it."""
    if len(a) != len(b):
        raise ValueError(f"dim mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@dataclass
class Enrollment:
    """The persisted enrolled identity. Written to a `*.face` file (gitignored —
    enrolled biometrics must never be committed)."""

    user: str
    dim: int
    threshold: float
    embeddings: List[List[float]]
    backend: str = "unknown"  # which engine produced these (they aren't portable)
    created: float = field(default_factory=time.time)
    version: int = ENROLL_VERSION

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "user": self.user,
                "backend": self.backend,
                "dim": self.dim,
                "threshold": self.threshold,
                "created": self.created,
                "embeddings": self.embeddings,
            }
        )

    @staticmethod
    def from_json(text: str) -> "Enrollment":
        d = json.loads(text)
        if d.get("version") != ENROLL_VERSION:
            raise ValueError(f"unsupported enrollment version {d.get('version')}")
        emb = d["embeddings"]
        if not emb:
            raise ValueError("enrollment has no embeddings")
        dim = d["dim"]
        for e in emb:
            if len(e) != dim:
                raise ValueError("embedding dim mismatch in enrollment file")
        return Enrollment(
            user=d["user"],
            dim=dim,
            threshold=d["threshold"],
            embeddings=emb,
            backend=d.get("backend", "unknown"),
            created=d.get("created", 0.0),
            version=d["version"],
        )

    def save(self, path: str) -> None:
        # 0600 and owned dir; enrolled faces are sensitive.
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, self.to_json().encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(path, 0o600)

    @staticmethod
    def load(path: str) -> "Enrollment":
        with open(path, "r", encoding="utf-8") as f:
            return Enrollment.from_json(f.read())


class Matcher:
    """Compares probe embeddings to an enrollment."""

    def __init__(self, enrollment: Enrollment):
        self.enrollment = enrollment
        self._normed = [l2_normalize(e) for e in enrollment.embeddings]

    def best_similarity(self, probe: List[float]) -> float:
        p = l2_normalize(probe)
        return max(cosine_similarity(p, e) for e in self._normed)

    def matches(self, probe: List[float]) -> bool:
        return self.best_similarity(probe) >= self.enrollment.threshold


@dataclass
class MatchAccumulator:
    """K-of-N sliding-window debounce over per-frame match booleans.

    `feed(matched)` returns True once at least `k` of the last `n` frames matched
    — so one spurious frame can't unlock, and one dropped frame can't lock you
    out mid-session. Also exposes `rejected` once it's mathematically impossible
    to reach k within the window, so a caller can stop early.
    """

    k: int = 3
    n: int = 5
    _window: List[bool] = field(default_factory=list)

    def __post_init__(self):
        if self.k > self.n or self.k <= 0:
            raise ValueError("require 0 < k <= n")

    def feed(self, matched: bool) -> bool:
        self._window.append(matched)
        if len(self._window) > self.n:
            self._window.pop(0)
        return self.accepted

    @property
    def accepted(self) -> bool:
        return sum(self._window) >= self.k

    @property
    def rejected(self) -> bool:
        # Full window and even counting every remaining slot as a hit can't reach k.
        hits = sum(self._window)
        remaining = self.n - len(self._window)
        return (hits + remaining) < self.k

    def reset(self) -> None:
        self._window.clear()


# ── self-test ────────────────────────────────────────────────────────────────

def _selftest() -> int:
    failures = 0

    def check(name, cond):
        nonlocal failures
        if not cond:
            failures += 1
            print(f"FAIL: {name}")

    # Build a tiny enrollment in a 4-d space.
    me = [l2_normalize(v) for v in ([1, 0, 0, 0.1], [0.9, 0.1, 0, 0])]
    enr = Enrollment(user="teo", dim=4, threshold=0.9, embeddings=me, backend="test")
    m = Matcher(enr)

    check("identical vector matches", m.matches([1, 0, 0, 0.1]))
    check("near vector matches", m.matches([0.95, 0.05, 0, 0.05]))
    check("orthogonal vector rejected", not m.matches([0, 1, 0, 0]))
    check("opposite vector rejected", not m.matches([-1, 0, 0, 0]))
    check("similarity in range", 0.99 <= m.best_similarity([1, 0, 0, 0.1]) <= 1.0001)

    # JSON round-trip + dim validation.
    enr2 = Enrollment.from_json(enr.to_json())
    check("json roundtrip", enr2.embeddings == enr.embeddings and enr2.threshold == 0.9)
    try:
        Enrollment.from_json('{"version":1,"user":"x","backend":"t","dim":3,'
                             '"threshold":0.5,"embeddings":[[1,0]]}')
        check("bad dim rejected", False)
    except ValueError:
        check("bad dim rejected", True)

    # File save/load with 0600.
    import tempfile
    path = os.path.join(tempfile.gettempdir(), f"applocker_selftest_{os.getpid()}.face")
    try:
        enr.save(path)
        mode = os.stat(path).st_mode & 0o777
        check("enrollment file is 0600", mode == 0o600)
        check("enrollment reloads", Matcher(Enrollment.load(path)).matches([1, 0, 0, 0.1]))
    finally:
        if os.path.exists(path):
            os.remove(path)

    # K-of-N accumulator.
    acc = MatchAccumulator(k=3, n=5)
    check("no accept before k", not any([acc.feed(True), acc.feed(True)]))
    check("accept at k", acc.feed(True))
    acc.reset()
    for b in [True, False, True, False]:
        acc.feed(b)
    check("2 of 4 not accepted (k=3)", not acc.accepted)
    acc.reset()
    for b in [False, False, False]:
        acc.feed(b)
    check("early reject when unreachable", acc.rejected)
    try:
        MatchAccumulator(k=6, n=5)
        check("invalid k<=n enforced", False)
    except ValueError:
        check("invalid k<=n enforced", True)

    if failures == 0:
        print("matcher self-test: all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("usage: python3 matcher.py --selftest")
