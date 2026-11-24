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

import glob
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

ENROLL_VERSION = 1
MAX_FACES = 5  # how many named face profiles a user may enrol


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
    """One enrolled face profile. Written to a `*.face` file (gitignored — enrolled
    biometrics must never be committed). `label` is the human name shown in the
    settings list ("me", "with glasses", "new haircut", …); several profiles can
    belong to the same person, and recognition matches against any of them."""

    user: str
    dim: int
    threshold: float
    embeddings: List[List[float]]
    backend: str = "unknown"  # which engine produced these (they aren't portable)
    label: str = ""  # display name; defaults to `user` if empty
    created: float = field(default_factory=time.time)
    version: int = ENROLL_VERSION

    def display_name(self) -> str:
        return self.label or self.user

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "user": self.user,
                "label": self.label,
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
            label=d.get("label", ""),
            created=d.get("created", 0.0),
            version=d["version"],
        )

    def save(self, path: str) -> None:
        # 0600 file in a 0700 dir; enrolled faces are sensitive (an unlistable
        # dir also hides how many profiles exist).
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, mode=0o700, exist_ok=True)
            try:
                os.chmod(d, 0o700)  # tighten a dir that pre-existed looser
            except OSError:
                pass
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


# ── multiple named face profiles ─────────────────────────────────────────────

def default_faces_dir() -> str:
    """Where named face profiles live ($APPLOCKER_FACES_DIR or the default)."""
    return os.path.expanduser(
        os.environ.get("APPLOCKER_FACES_DIR", "~/.config/applocker/faces")
    )


def legacy_enrollment_path() -> str:
    """The single-profile path from before multi-face — still honoured."""
    return os.path.expanduser(
        os.environ.get("APPLOCKER_FACE_ENROLLMENT", "~/.config/applocker/owner.face")
    )


def slugify(name: str) -> str:
    """A safe `*.face` filename stem from a display name."""
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "face"


def list_profiles(faces_dir: Optional[str] = None,
                  legacy: Optional[str] = None) -> List[Tuple[str, "Enrollment"]]:
    """All enrolled profiles as (path, Enrollment), from the faces dir plus the
    legacy single-file location. Unreadable files are skipped, not fatal."""
    faces_dir = faces_dir if faces_dir is not None else default_faces_dir()
    legacy = legacy if legacy is not None else legacy_enrollment_path()
    out: List[Tuple[str, Enrollment]] = []
    if legacy and os.path.exists(legacy):
        try:
            out.append((legacy, Enrollment.load(legacy)))
        except (OSError, ValueError):
            pass
    if os.path.isdir(faces_dir):
        for p in sorted(glob.glob(os.path.join(faces_dir, "*.face"))):
            try:
                out.append((p, Enrollment.load(p)))
            except (OSError, ValueError):
                pass
    return out


def pooled(enrollments: List["Enrollment"], backend: Optional[str] = None) -> "Enrollment":
    """Pool several profiles' embeddings into one Enrollment so a single Matcher
    matches against *any* enrolled face. Only profiles matching `backend` (and its
    dim) are pooled — embeddings from different engines aren't comparable."""
    usable = [e for e in enrollments if e.embeddings]
    if backend is not None:
        usable = [e for e in usable if e.backend == backend]
    if not usable:
        raise ValueError("no usable enrolled faces for this backend")
    dim = usable[0].dim
    emb: List[List[float]] = []
    for e in usable:
        if e.dim == dim:
            emb.extend(e.embeddings)
    return Enrollment(
        user="*",
        label="pooled",
        dim=dim,
        threshold=min(e.threshold for e in usable),
        embeddings=emb,
        backend=usable[0].backend,
    )


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

    # File save/load with 0600, and the label survives the round-trip.
    import tempfile
    path = os.path.join(tempfile.gettempdir(), f"applocker_selftest_{os.getpid()}.face")
    try:
        enr.label = "new haircut"
        enr.save(path)
        mode = os.stat(path).st_mode & 0o777
        check("enrollment file is 0600", mode == 0o600)
        reloaded = Enrollment.load(path)
        check("label round-trips", reloaded.display_name() == "new haircut")
        check("enrollment reloads", Matcher(reloaded).matches([1, 0, 0, 0.1]))
    finally:
        if os.path.exists(path):
            os.remove(path)

    # Multi-profile helpers: slugify + match-against-any via pooling.
    check("slugify", slugify("New Haircut!") == "new-haircut")
    check("slugify empty", slugify("  ") == "face")
    me2 = Enrollment(user="teo", label="glasses", dim=4, threshold=0.9,
                     embeddings=[l2_normalize([0, 1, 0, 0.1])], backend="test")
    pool = Matcher(pooled([enr, me2], backend="test"))
    check("pooled matches profile A", pool.matches([1, 0, 0, 0.1]))
    check("pooled matches profile B", pool.matches([0, 1, 0, 0.1]))
    check("pooled rejects stranger", not pool.matches([0, 0, 1, 0]))
    try:
        pooled([enr], backend="different-backend")
        check("pooled needs matching backend", False)
    except ValueError:
        check("pooled needs matching backend", True)

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
