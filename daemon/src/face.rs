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
use std::path::PathBuf;
use std::process::{Command, Stdio};

use crate::auth::{Config, FaceVerifier, NoFace};

/// A face verifier backed by one run of `recognize.py`. Returns `true` only on a
/// `match` line — every other outcome (nomatch/noface/nolive/error) is a miss,
/// so the routine falls through to the PIN/sudo prompt rather than failing open.
pub struct SubprocessFace {
    script: PathBuf,
    enrollment: PathBuf,
}

impl SubprocessFace {
    pub fn new() -> SubprocessFace {
        SubprocessFace {
            script: locate_recognize_script(),
            enrollment: enrollment_path(),
        }
    }
}

impl Default for SubprocessFace {
    fn default() -> Self {
        Self::new()
    }
}

impl FaceVerifier for SubprocessFace {
    fn try_match(&mut self) -> bool {
        let mut cmd = Command::new("python3");
        cmd.arg(&self.script)
            .arg("--enrollment")
            .arg(&self.enrollment)
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit()); // progress/challenge text goes to our log

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
pub fn build() -> (Box<dyn FaceVerifier>, u32) {
    let enabled = match std::env::var("APPLOCKER_FACE").ok().as_deref() {
        Some("1") => true,
        Some("0") => false,
        _ => crate::policy::load_default().face_enabled,
    };
    if enabled && enrollment_path().is_file() {
        (Box::new(SubprocessFace::new()), 1)
    } else {
        if enabled {
            eprintln!(
                "applockerd: face enabled but no enrollment at {} — using PIN/sudo only",
                enrollment_path().display()
            );
        }
        (Box::new(NoFace), 1)
    }
}

/// A [`Config`] with the face-attempt count set for the chosen verifier.
pub fn config_for(attempts: u32) -> Config {
    Config {
        face_attempts: attempts,
        ..Config::default()
    }
}

/// `$APPLOCKER_FACE_ENROLLMENT`, else the invoking user's
/// `~/.config/applocker/owner.face`. The daemon runs as root, so we resolve the
/// *invoking* user's home (via `SUDO_USER`) rather than root's.
fn enrollment_path() -> PathBuf {
    if let Some(p) = std::env::var_os("APPLOCKER_FACE_ENROLLMENT") {
        return PathBuf::from(p);
    }
    let home = invoking_user_home().unwrap_or_else(|| PathBuf::from("/root"));
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
