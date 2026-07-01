#!/usr/bin/env python3
"""Download the SFace ONNX model for the strong recognition backend.

Run once, on a machine with network:

    python3 face/fetch_models.py

Saves the model into $APPLOCKER_MODELS (default ~/.config/applocker/models/).
Once present, `engine.build_engine()` auto-selects the SFace backend; until then
it falls back to the Haar-pixel v0. This only affects *unlock* recognition — the
presence/attention tier is lenient and identity-blind regardless.

Detection is Haar, not YuNet, so we only need SFace: the current OpenCV-Zoo YuNet
model needs OpenCV ≥ 4.7 and fails to load on Ubuntu's 4.6.

Note: OpenCV Zoo stores models with git-LFS, so we fetch from the `media.`
GitHub host (the `raw.` host returns a tiny LFS *pointer*, not the model). We
sanity-check the downloaded size to catch that.
"""

from __future__ import annotations

import os
import sys
import urllib.request

from engine import SFACE_MODEL, model_dir

BASE = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
SOURCES = {
    SFACE_MODEL: f"{BASE}/face_recognition_sface/{SFACE_MODEL}",
}
MIN_BYTES = 50_000  # both models are >>50 KB; smaller means we got an LFS pointer


def fetch(name: str, url: str, dest_dir: str) -> bool:
    dest = os.path.join(dest_dir, name)
    if os.path.exists(dest) and os.path.getsize(dest) >= MIN_BYTES:
        print(f"  {name}: already present ({os.path.getsize(dest)//1024} KB)")
        return True
    print(f"  {name}: downloading…")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "applocker-fetch"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except Exception as e:  # network / URL error
        print(f"    FAILED: {e}", file=sys.stderr)
        return False
    if len(data) < MIN_BYTES:
        print(f"    FAILED: got {len(data)} bytes (an LFS pointer?), not the model",
              file=sys.stderr)
        return False
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    print(f"    saved {len(data)//1024} KB -> {dest}")
    return True


def main() -> int:
    d = model_dir()
    os.makedirs(d, exist_ok=True)
    print(f"Fetching recognition models into {d}")
    ok = all(fetch(name, url, d) for name, url in SOURCES.items())
    if ok:
        print("Done. `engine.build_engine()` will now use the SFace backend.")
        print("Re-enroll after switching backends: python3 face/enroll.py")
        return 0
    print("\nSome downloads failed. You can also grab them manually from")
    print("https://github.com/opencv/opencv_zoo (models/face_detection_yunet,")
    print(f"models/face_recognition_sface) and drop them in {d}.")
    return 1


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())
