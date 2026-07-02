//! Face tier wiring: the daemon-side [`FaceVerifier`] that shells out to the
//! Python recognizer (`face/recognize.py`), and env-based selection between it
//! and the [`NoFace`] stub.
//!
//! The Python side runs the *whole* face routine in one invocation — camera
//! capture, the liveness challenge, and K-of-N matching — and prints one word on
//! stdout (`match` / `nomatch` / `noface` / `nolive`). So one `try_match()` call
//! maps to one subprocess run, and the daemon's own face-attempt loop is set to
//! a single attempt (see [`build`]).
//!
//! **Opt-in:** unless `APPLOCKER_FACE=1`, the daemon uses `NoFace` and goes
//! straight to the PIN/sudo fallback. This keeps everything working before the
//! OpenCV stack is installed and enrollment exists; face is switched on only
//! once the user has run `probe_env.py` → `enroll.py`.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use crate::auth::{Config, FaceVerifier, NoFace};

/// A face verifier backed by one run of `recognize.py`. Returns `true` only on a
/// `match` line — every other outcome (nomatch/noface/nolive/error) is a miss,
/// so the routine falls through to the PIN/sudo prompt rather than failing open.
///
/// `home` is the user whose enrolled faces/models we use and (when we're the
/// root service) whose session we drop the camera process into.
pub struct SubprocessFace {
    script: PathBuf,
    home: PathBuf,
}

impl SubprocessFace {
    pub fn new(home: PathBuf) -> SubprocessFace {
        SubprocessFace {
            script: locate_recognize_script(),
            home,
        }
    }
}

impl FaceVerifier for SubprocessFace {
    fn try_match(&mut self) -> bool {
        let mut cmd = Command::new("python3");
        // The daemon runs as root but the ONNX models live in the *user's*
        // home — point the engine there explicitly, or it silently falls back
        // to the weak haar-pixel backend (which the sface profiles refuse).
        if std::env::var_os("APPLOCKER_MODELS").is_none() {
            cmd.env("APPLOCKER_MODELS", self.home.join(".config/applocker/models"));
        }
        cmd.arg(&self.script)
            .arg("--faces-dir")
            .arg(faces_dir(&self.home))
            .arg("--enrollment") // legacy single-file profile, if present
            .arg(legacy_enrollment_path(&self.home))
            // Short per-run budget: the daemon retries the whole run (3×, 1s
            // apart — see build()), so each run can give up quickly instead of
            // camping on the camera for 15s.
            .arg("--timeout")
            .arg("5")
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit()); // progress/challenge text goes to our log

        // App/file unlock is **recognition only** — fast and frictionless, no
        // gesture. Liveness is reserved for the login/sudo/screen-unlock tier
        // (the future PAM path sets APPLOCKER_FACE_LIVENESS=1). See the tier note
        // in ../../face/README.md: a photo opening an app is low-stakes for a
        // casual fence, and PIN/sudo remain as fallback.
        if std::env::var("APPLOCKER_FACE_LIVENESS").ok().as_deref() != Some("1") {
            cmd.arg("--no-liveness");
        }

        // As the root service, run the camera capture in the user's session
        // (their DISPLAY for any guided window, and the `video` group).
        crate::session::attach(&mut cmd);

        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                eprintln!("applockerd: failed to launch face recognizer: {e}");
                return false;
            }
        };
        let mut out = String::new();
        if let Some(mut stdout) = child.stdout.take() {
            let _ = stdout.read_to_string(&mut out);
        }
        let _ = child.wait();
        out.lines().next().map(str::trim) == Some("match")
    }
}

/// Which face verifier to use, plus how many attempts the routine should make.
///
/// Face is enabled when the saved [`Policy`](crate::policy::Policy) has `face =
/// on` **and** an enrollment exists. `$APPLOCKER_FACE` overrides the policy for
/// quick testing (`1` forces on, `0` forces off). When on, the subprocess
/// recognizer runs once (the Python side loops internally); otherwise the
/// always-decline stub sends the routine straight to PIN/sudo.
/// The third element is whether the *real* recognizer is in play (drives the
/// on-screen feedback window — no window when face is off).
pub fn build() -> (Box<dyn FaceVerifier>, u32, bool) {
    let enabled = match std::env::var("APPLOCKER_FACE").ok().as_deref() {
        Some("1") => true,
        Some("0") => false,
        _ => crate::policy::load_default().face_enabled,
    };
    let home = effective_home();
    if enabled && has_enrollment(&home) {
        // 3 subprocess runs, 1s apart (user: one miss shouldn't end it) — each
        // run is a 5s camera window, so worst case ≈ 17s before PIN/sudo.
        (Box::new(SubprocessFace::new(home)), 3, true)
    } else {
        if enabled {
            eprintln!(
                "applockerd: face enabled but no enrolled faces in {} — using PIN/sudo only",
                faces_dir(&home).display()
            );
        }
        (Box::new(NoFace), 1, false)
    }
}

/// The home directory whose enrolled faces/models we use. For the root service
/// that's the active session user's home; otherwise the invoking user's.
fn effective_home() -> PathBuf {
    if unsafe { libc::geteuid() } == 0 {
        if let Some(ctx) = crate::session::SessionCtx::discover() {
            return ctx.home;
        }
    }
    invoking_user_home().unwrap_or_else(|| PathBuf::from("/root"))
}

/// True if the user has at least one enrolled face (a `*.face` in the faces dir,
/// or the legacy single-file profile).
fn has_enrollment(home: &Path) -> bool {
    if legacy_enrollment_path(home).is_file() {
        return true;
    }
    std::fs::read_dir(faces_dir(home))
        .map(|mut d| {
            d.any(|e| {
                e.ok()
                    .map(|e| e.path().extension().and_then(|x| x.to_str()) == Some("face"))
                    .unwrap_or(false)
            })
        })
        .unwrap_or(false)
}

/// A [`Config`] with the face-attempt count set for the chosen verifier.
pub fn config_for(attempts: u32) -> Config {
    Config {
        face_attempts: attempts,
        face_gap: std::time::Duration::from_secs(1),
        ..Config::default()
    }
}

/// The named-profiles directory (`$APPLOCKER_FACES_DIR`, else
/// `<home>/.config/applocker/faces`).
fn faces_dir(home: &Path) -> PathBuf {
    if let Some(p) = std::env::var_os("APPLOCKER_FACES_DIR") {
        return PathBuf::from(p);
    }
    home.join(".config/applocker/faces")
}

/// The legacy single-file profile (`$APPLOCKER_FACE_ENROLLMENT`, else
/// `<home>/.config/applocker/owner.face`).
fn legacy_enrollment_path(home: &Path) -> PathBuf {
    if let Some(p) = std::env::var_os("APPLOCKER_FACE_ENROLLMENT") {
        return PathBuf::from(p);
    }
    home.join(".config/applocker/owner.face")
}

fn invoking_user_home() -> Option<PathBuf> {
    // Prefer the sudo-invoking user's home; fall back to $HOME.
    if let Ok(user) = std::env::var("SUDO_USER") {
        if !user.is_empty() {
            return Some(PathBuf::from(format!("/home/{user}")));
        }
    }
    std::env::var_os("HOME").map(PathBuf::from)
}

fn locate_recognize_script() -> PathBuf {
    if let Ok(p) = std::env::var("APPLOCKER_FACE_RECOGNIZE") {
        let path = PathBuf::from(p);
        if path.is_file() {
            return path;
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let repo = dir.join("../../../face/recognize.py");
            if repo.is_file() {
                return repo;
            }
        }
    }
    let installed = PathBuf::from("/usr/lib/applocker/recognize.py");
    if installed.is_file() {
        return installed;
    }
    PathBuf::from("face/recognize.py")
}
