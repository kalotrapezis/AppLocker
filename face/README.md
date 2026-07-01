# AppLocker face pipeline (step 3)

The recognition + liveness tier — "is this *me*, live at the camera?". It answers
the face part of the auth routine; the Rust daemon calls it and, on anything
other than a clean match, falls through to the PIN/sudo prompt.

Per the user's decision, **liveness is required**: a blink + a random head turn
must be completed, so a printed photo or a phone screen can't unlock. On a plain
RGB webcam this challenge–response is the bar we can raise in software — it is
*not* claimed to be spoof-proof.

## Modules

| File | Needs camera/OpenCV? | What |
|------|:--:|------|
| `liveness.py` | no | the blink/turn challenge **state machine** (pure logic, self-tested) |
| `matcher.py` | no | enrolled-embedding store + cosine match + K-of-N debounce (pure logic, self-tested) |
| `engine.py` | yes | the only OpenCV module: frames → signals + embeddings |
| `enroll.py` | yes | capture the owner's face → `owner.face` |
| `recognize.py` | yes | run the full routine once; print `match`/`nomatch`/`noface`/`nolive` |
| `probe_env.py` | yes | report what the environment supports |

The two pure-logic modules have offline self-tests — no camera, OpenCV, or
display needed:

```bash
python3 face/liveness.py --selftest
python3 face/matcher.py  --selftest
```

## Provisioning (do this first)

This box has no dlib/MediaPipe and no pip/network, so we use OpenCV from apt:

```bash
sudo apt install python3-opencv python3-numpy opencv-data python3-scipy v4l-utils
python3 face/probe_env.py          # confirm cascades + camera, pick the backend
```

## Enroll, then test

```bash
# teach it your face (writes ~/.config/applocker/owner.face, 0600)
python3 face/enroll.py --user "$USER"

# run the full liveness+match routine once (prints one word on stdout)
python3 face/recognize.py
# stderr shows the challenge ("Blink then Turn your head right"); do it.
```

Then switch it on in the daemon (opt-in — off by default):

```bash
# auth-test uses face when APPLOCKER_FACE=1 and an enrollment exists:
APPLOCKER_FACE=1 ./daemon/target/release/applockerd auth-test firefox
```

## Daemon protocol

`recognize.py` writes exactly one line on **stdout**, then exits:

| line | exit | meaning |
|------|:--:|---------|
| `match`   | 0 | live **and** recognised → face factor passes |
| `nomatch` | 1 | live but not you |
| `noface`  | 3 | never saw a usable face / camera failed |
| `nolive`  | 4 | face seen but liveness not proven in time |

stderr is human progress text (safe to log). The Rust side
(`daemon/src/face.rs`) treats only `match` as success; everything else falls
through to the PIN/sudo prompt, so a broken camera never locks you out.

## Two tiers, two strengths (important)

Recognition and presence are **deliberately different strengths**:

- **Recognition (unlock) — strong.** "Is this *me*?" Runs only when you unlock.
- **Presence (attention) — lenient, identity-blind.** "Is *a* face there?" Runs
  continuously while unlocked (`attention.py`). It never checks *who* — so it
  doesn't react to a passer-by, and a glance down at the screen doesn't lock you
  (any detected frame resets the absence clock). Using the strong identity model
  here would fight you constantly; that's the whole point of the split.

## Recognition backends

Two, auto-selected by `build_engine()`:

1. **`yunet-sface` (strong, recommended)** — YuNet detection + SFace 128-d
   embeddings, built into apt OpenCV 4.6 (`cv2.FaceDetectorYN` /
   `cv2.FaceRecognizerSF`). Needs two ONNX models; fetch them once:

   ```bash
   python3 face/fetch_models.py      # into ~/.config/applocker/models/
   ```

   Selected automatically when the models are present.

2. **`haar-pixel-v0` (fallback)** — a pixel-template embedding (flattened
   grayscale crop). Zero downloads, but lighting/pose-sensitive. Used until the
   SFace models are in place.

Switching backends only changes the engine and the enrollment's `backend` tag —
liveness, the matcher, and the daemon protocol are all backend-agnostic.
**Re-enroll after switching** (`enroll.py`); embeddings aren't comparable across
backends, and `recognize.py` warns on a mismatch.

**Liveness** is Haar-based (`opencv-data`) in both backends — eyes-open via the
eye cascade, yaw via the profile cascade — enough to drive blink + turn.

## Security note

Face-as-login on an RGB webcam is spoofable by a good photo/video; liveness
raises the bar but doesn't eliminate it. This is acceptable for AppLocker's
threat model (a privacy fence against casual local access), and the password
fallback is always present. Don't oversell it as biometric security.
