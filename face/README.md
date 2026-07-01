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

## Recognition backend, honestly

- **Liveness** is fully supported today with Haar cascades (`opencv-data`), no
  downloads. It's coarse — eyes-open via the eye cascade, yaw via the profile
  cascade — but enough to drive blink + turn.
- **Recognition** is the weak spot without dlib/MediaPipe. The shipped engine
  (`HaarPixelEngine`, `haar-pixel-v0`) uses a **pixel-template** embedding
  (aligned, equalised, flattened grayscale crop). It runs with zero downloads
  and is fine as a first cut behind liveness + PIN/sudo, but it is
  lighting/pose-sensitive and **not** a strong recognizer.

### Upgrading recognition (recommended when network is available)

OpenCV 4.6 exposes `cv2.FaceDetectorYN` (YuNet) and `cv2.FaceRecognizerSF`
(SFace) — a proper 128-d face embedder. It needs two ONNX model files that
aren't bundled:

- `face_detection_yunet_2023mar.onnx`
- `face_recognition_sface_2021dec.onnx`

Place them in `~/.config/applocker/models/` (probe_env.py checks for them) and we
add an `SFaceEngine` to `engine.py`. **Nothing else changes** — the enrollment
format, matcher, liveness, and daemon protocol are all backend-agnostic; only the
engine and the stored `backend` tag differ. Re-enroll after switching backends
(embeddings aren't comparable across engines).

## Security note

Face-as-login on an RGB webcam is spoofable by a good photo/video; liveness
raises the bar but doesn't eliminate it. This is acceptable for AppLocker's
threat model (a privacy fence against casual local access), and the password
fallback is always present. Don't oversell it as biometric security.
