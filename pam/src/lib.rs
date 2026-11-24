//! `pam_applocker.so` — face login for LightDM, cinnamon-screensaver and sudo.
//!
//! Installed as `auth sufficient` *above* the password line, so:
//!   face matches (with the head-turn liveness challenge) → logged in;
//!   anything else — no camera, no enrollment, timeout, nomatch, crash —
//!   → PAM_AUTH_ERR, and the stack falls through to the normal password.
//!   **This module can never lock you out and never replaces the password.**
//!
//! ```text
//! # /etc/pam.d/lightdm  (also: cinnamon-screensaver, sudo) — FIRST auth line:
//! auth sufficient pam_applocker.so
//! ```
//!
//! It shells out to the same recognizer the app-gate uses (`recognize.py`),
//! with `APPLOCKER_FACE_LIVENESS=1` — the login tier *requires* the turn
//! challenge, so a printed photo can't log in (the user's explicit decision).
//! Module options (all optional):
//!
//!   script=/path/to/recognize.py    default: /usr/lib/applocker/recognize.py
//!   timeout=20                      hard kill for the child, seconds
//!
//! Everything here fails **closed** (to the next PAM module, not into the
//! session): unknown user → AUTH_ERR, no faces dir → AUTH_ERR, panic → abort
//! (PAM treats a died conversation as failure).

use std::ffi::{c_char, c_int, c_void, CStr, CString};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const PAM_SUCCESS: c_int = 0;
const PAM_AUTH_ERR: c_int = 7;

// ── libpam via dlopen (no link-time dependency, like daemon/src/pam.rs) ─────

type PamGetUser =
    unsafe extern "C" fn(*mut c_void, *mut *const c_char, *const c_char) -> c_int;

fn pam_get_user(pamh: *mut c_void) -> Option<String> {
    unsafe {
        let lib = libc_dlopen(b"libpam.so.0\0");
        if lib.is_null() {
            return None;
        }
        let sym = libc_dlsym(lib, b"pam_get_user\0");
        if sym.is_null() {
            return None;
        }
        let get_user: PamGetUser = std::mem::transmute(sym);
        let mut user: *const c_char = std::ptr::null();
        if get_user(pamh, &mut user, std::ptr::null()) != PAM_SUCCESS || user.is_null() {
            return None;
        }
        CStr::from_ptr(user).to_str().ok().map(|s| s.to_string())
    }
}

extern "C" {
    #[link_name = "dlopen"]
    fn dlopen_raw(filename: *const c_char, flag: c_int) -> *mut c_void;
    #[link_name = "dlsym"]
    fn dlsym_raw(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
}

unsafe fn libc_dlopen(name: &[u8]) -> *mut c_void {
    dlopen_raw(name.as_ptr() as *const c_char, 2 /* RTLD_NOW */)
}

unsafe fn libc_dlsym(handle: *mut c_void, name: &[u8]) -> *mut c_void {
    dlsym_raw(handle, name.as_ptr() as *const c_char)
}

// ── module options ───────────────────────────────────────────────────────────

struct Options {
    script: PathBuf,
    timeout: Duration,
    /// `ui` module arg: ask the recognizer to show the guided camera window
    /// (screensaver tier — a display exists). It falls back to headless on its
    /// own if there isn't one, so this is always safe to set.
    ui: bool,
    /// `priority=` module arg → the camera-arbitration level recognize.py runs
    /// at (lockscreen|app|file|presence). None = unarbitrated (the sudo tier
    /// runs in root's session and never contends with the desktop helpers).
    priority: Option<String>,
}

fn parse_options(argc: c_int, argv: *const *const c_char) -> Options {
    let mut opts = Options {
        script: PathBuf::from("/usr/lib/applocker/recognize.py"),
        timeout: Duration::from_secs(20),
        ui: false,
        priority: None,
    };
    if argv.is_null() {
        return opts;
    }
    for i in 0..argc {
        let p = unsafe { *argv.offset(i as isize) };
        if p.is_null() {
            continue;
        }
        let Ok(arg) = unsafe { CStr::from_ptr(p) }.to_str() else { continue };
        if let Some(v) = arg.strip_prefix("script=") {
            opts.script = PathBuf::from(v);
        } else if let Some(v) = arg.strip_prefix("timeout=") {
            if let Ok(s) = v.parse::<u64>() {
                opts.timeout = Duration::from_secs(s.clamp(5, 120));
            }
        } else if let Some(v) = arg.strip_prefix("priority=") {
            opts.priority = Some(v.to_string());
        } else if arg == "ui" {
            opts.ui = true;
        }
    }
    opts
}

// ── the auth decision ────────────────────────────────────────────────────────

fn authenticate(user: &str, opts: &Options) -> c_int {
    // Sanity on the user name before it goes anywhere near a path.
    if user.is_empty()
        || user == "root"
        || !user.chars().all(|c| c.is_ascii_alphanumeric() || "-_.".contains(c))
    {
        return PAM_AUTH_ERR;
    }
    let home = PathBuf::from(format!("/home/{user}"));
    let faces = home.join(".config/applocker/faces");
    let legacy = home.join(".config/applocker/owner.face");
    let models = home.join(".config/applocker/models");
    let has_faces = Path::new(&legacy).is_file()
        || std::fs::read_dir(&faces)
            .map(|mut d| {
                d.any(|e| {
                    e.ok()
                        .map(|e| e.path().extension().and_then(|x| x.to_str()) == Some("face"))
                        .unwrap_or(false)
                })
            })
            .unwrap_or(false);
    if !has_faces || !opts.script.is_file() {
        return PAM_AUTH_ERR; // not enrolled / not installed → next module
    }

    let mut cmd = Command::new("/usr/bin/python3");
    cmd.arg(&opts.script)
        .arg("--faces-dir")
        .arg(&faces)
        .arg("--enrollment")
        .arg(&legacy)
        .arg("--timeout")
        .arg(format!("{}", opts.timeout.as_secs().saturating_sub(2).max(5)))
        .env("APPLOCKER_FACE_LIVENESS", "1") // login tier: liveness REQUIRED
        .env("APPLOCKER_MODELS", &models)
        .env("APPLOCKER_UI", if opts.ui { "1" } else { "0" })
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit()); // challenge text lands in the PAM app's log
    if let Some(pri) = &opts.priority {
        // Arbitrate the webcam against the desktop helpers (see face/cameralock.py).
        cmd.env("APPLOCKER_CAMERA_PRIORITY", pri);
    }
    let child = cmd.spawn();
    let mut child = match child {
        Ok(c) => c,
        Err(_) => return PAM_AUTH_ERR,
    };

    // Hard deadline: poll, then kill. A PAM module must never hang the stack.
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                if !status.success() {
                    return PAM_AUTH_ERR;
                }
                break;
            }
            Ok(None) => {
                if started.elapsed() > opts.timeout {
                    let _ = child.kill();
                    let _ = child.wait();
                    return PAM_AUTH_ERR;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            Err(_) => return PAM_AUTH_ERR,
        }
    }

    // Exit 0 alone isn't enough — require the literal `match` on stdout.
    let mut out = String::new();
    if let Some(mut stdout) = child.stdout.take() {
        use std::io::Read;
        let _ = stdout.read_to_string(&mut out);
    }
    if out.lines().next().map(str::trim) == Some("match") {
        PAM_SUCCESS
    } else {
        PAM_AUTH_ERR
    }
}

// ── PAM entry points ─────────────────────────────────────────────────────────

/// # Safety
/// Called by the PAM stack with a valid `pamh` and argv of length `argc`.
#[no_mangle]
pub unsafe extern "C" fn pam_sm_authenticate(
    pamh: *mut c_void,
    _flags: c_int,
    argc: c_int,
    argv: *const *const c_char,
) -> c_int {
    let result = std::panic::catch_unwind(|| {
        let opts = parse_options(argc, argv);
        match pam_get_user(pamh) {
            Some(user) => authenticate(&user, &opts),
            None => PAM_AUTH_ERR,
        }
    });
    result.unwrap_or(PAM_AUTH_ERR)
}

/// Credentials are not our business; always succeed so `sufficient` works.
#[no_mangle]
pub unsafe extern "C" fn pam_sm_setcred(
    _pamh: *mut c_void,
    _flags: c_int,
    _argc: c_int,
    _argv: *const *const c_char,
) -> c_int {
    PAM_SUCCESS
}

// keep CString referenced so the import isn't "unused" if options grow
#[allow(dead_code)]
fn _keep(_: CString) {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_bad_users() {
        let opts = Options {
            script: PathBuf::from("/nonexistent"),
            timeout: Duration::from_secs(5),
            ui: false,
        };
        assert_eq!(authenticate("", &opts), PAM_AUTH_ERR);
        assert_eq!(authenticate("root", &opts), PAM_AUTH_ERR);
        assert_eq!(authenticate("../etc", &opts), PAM_AUTH_ERR);
        assert_eq!(authenticate("a b", &opts), PAM_AUTH_ERR);
        // Well-formed but unenrolled/uninstalled user also falls through.
        assert_eq!(authenticate("no-such-user-xyz", &opts), PAM_AUTH_ERR);
    }
}
