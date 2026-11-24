//! Unlock feedback window — the daemon side of `gui/unlock_feedback.py`.
//!
//! While the face recognizer runs, the user should see *something* (the unlock
//! is otherwise a silent pause). [`Feedback`] holds the little status window's
//! stdin pipe and pushes state lines at it (`scanning` / `fail n total` / `ok` /
//! `close`). [`FaceWithFeedback`] and [`ClosingPrompter`] are thin decorators
//! around the existing [`FaceVerifier`]/[`Prompter`] traits, so `auth::run`
//! stays untouched and fully unit-testable.
//!
//! Everything here fails soft: if the window can't spawn (no display, missing
//! script), auth proceeds exactly as before — feedback is cosmetic, never a
//! gate.

use std::cell::RefCell;
use std::io::Write;
use std::time::Duration;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::rc::Rc;

use crate::auth::{Available, FaceVerifier, Prompter, PromptResult, Fallback};

/// A handle on one feedback-window process. Dropping it closes the window
/// (stdin EOF), so it can never outlive the auth attempt.
pub struct Feedback {
    child: Option<Child>,
}

impl Feedback {
    /// Spawn the window for `app_name`. On any failure returns a dead handle —
    /// the send methods just no-op.
    pub fn spawn(app_name: &str) -> Feedback {
        let mut cmd = Command::new("python3");
        cmd.arg(locate_feedback_script())
            .arg("--app")
            .arg(app_name)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::inherit());
        // Show the window in the user's session when we're the root service.
        crate::session::attach(&mut cmd);
        let child = cmd
            .spawn()
            .map_err(|e| eprintln!("applockerd: no feedback window: {e}"))
            .ok();
        Feedback { child }
    }

    /// A handle that never shows anything (face tier disabled).
    pub fn none() -> Feedback {
        Feedback { child: None }
    }

    /// Is a window actually on screen?
    pub fn is_live(&self) -> bool {
        self.child.is_some()
    }

    fn send(&mut self, line: &str) {
        if let Some(child) = self.child.as_mut() {
            if let Some(stdin) = child.stdin.as_mut() {
                let _ = writeln!(stdin, "{line}");
                let _ = stdin.flush();
            }
        }
    }

    pub fn scanning(&mut self) {
        self.send("scanning");
    }

    /// "Touch the fingerprint reader" — the reader is waiting, silently.
    pub fn finger(&mut self) {
        self.send("finger");
    }

    pub fn fail(&mut self, attempt: u32, total: u32) {
        self.send(&format!("fail {attempt} {total}"));
    }

    /// Show the green check, and let it be *seen*: the window displays the ✓ for
    /// ~700ms and quits by itself, so we wait instead of reaping.
    ///
    /// This used to `send("ok")` then `reap()` straight away — and reap drops
    /// the child's stdin, whose EOF makes the window quit at once. The tick was
    /// therefore never on screen for more than a flash, which is exactly why a
    /// successful face unlock looked like nothing had happened and the lock
    /// looked broken. Costs ~0.9s on a successful unlock; that is the feature.
    pub fn ok(&mut self) {
        if !self.is_live() {
            return;
        }
        self.send("ok");
        std::thread::sleep(OK_LINGER);
        self.reap();
    }

    /// Show ✕ and the reason, long enough to read, then let the window go.
    /// Used when the routine refuses outright and there is no prompt to
    /// explain itself — otherwise the app simply fails to open, silently.
    pub fn denied(&mut self, why: &str) {
        if !self.is_live() {
            return;
        }
        self.send(&format!("denied {why}"));
        std::thread::sleep(DENIED_LINGER);
        self.reap();
    }

    pub fn close(&mut self) {
        self.send("close");
        self.reap();
    }

    fn reap(&mut self) {
        if let Some(mut child) = self.child.take() {
            drop(child.stdin.take()); // EOF backs up the `close`/`ok` line
            let _ = child.wait();
        }
    }
}

/// How long to leave the ✓ on screen. Must outlast the window's own
/// `OK_VISIBLE_MS` self-close timer (see gui/unlock_feedback.py), with slack.
const OK_LINGER: Duration = Duration::from_millis(1400);

/// Must outlast the window's own `DENIED_VISIBLE_MS` (2200ms).
const DENIED_LINGER: Duration = Duration::from_millis(2400);

impl Drop for Feedback {
    fn drop(&mut self) {
        self.close();
    }
}

/// Shared handle: the face wrapper and the prompter wrapper both talk to the
/// same window (the prompter must close it before its dialog appears).
pub type SharedFeedback = Rc<RefCell<Feedback>>;

/// Wraps a [`FaceVerifier`]: announces `scanning` before each attempt and
/// `fail n/total` after each miss. On a match it flashes the green check.
pub struct FaceWithFeedback {
    inner: Box<dyn FaceVerifier>,
    fb: SharedFeedback,
    attempt: u32,
    total: u32,
}

impl FaceWithFeedback {
    pub fn new(inner: Box<dyn FaceVerifier>, fb: SharedFeedback, total_attempts: u32) -> Self {
        FaceWithFeedback { inner, fb, attempt: 0, total: total_attempts }
    }
}

impl FaceVerifier for FaceWithFeedback {
    fn try_match(&mut self) -> bool {
        self.attempt += 1;
        self.fb.borrow_mut().scanning();
        let matched = self.inner.try_match();
        if matched {
            self.fb.borrow_mut().ok();
        } else {
            self.fb.borrow_mut().fail(self.attempt, self.total);
        }
        matched
    }
}

/// Wraps a [`Prompter`]: the first time the PIN/sudo dialog is about to show,
/// close the feedback window (after a beat, so the last shake/✕ is visible).
/// Wraps the real [`Fallback`] so the reader's silent wait is visible: the
/// window says "Touch the fingerprint reader" while PAM blocks, and shows the ✓
/// if the touch is accepted. Everything else passes straight through.
///
/// Without this the fingerprint tier is a few seconds of nothing at all — which
/// is how a working reader ends up looking broken.
pub struct FallbackWithFeedback<F: Fallback> {
    inner: F,
    fb: SharedFeedback,
}

impl<F: Fallback> FallbackWithFeedback<F> {
    pub fn new(inner: F, fb: SharedFeedback) -> Self {
        FallbackWithFeedback { inner, fb }
    }
}

impl<F: Fallback> Fallback for FallbackWithFeedback<F> {
    fn pin_available(&self) -> bool {
        self.inner.pin_available()
    }
    fn sudo_available(&self) -> bool {
        self.inner.sudo_available()
    }
    fn verify_pin(&self, secret: &str) -> bool {
        self.inner.verify_pin(secret)
    }
    fn verify_password(&self, secret: &str) -> Result<bool, String> {
        self.inner.verify_password(secret)
    }
    fn fingerprint_available(&self) -> bool {
        self.inner.fingerprint_available()
    }

    fn verify_fingerprint(&self) -> Result<bool, String> {
        self.fb.borrow_mut().finger();
        let out = self.inner.verify_fingerprint();
        if matches!(out, Ok(true)) {
            self.fb.borrow_mut().ok();
        }
        out
    }
}

pub struct ClosingPrompter<P: Prompter> {
    inner: P,
    fb: SharedFeedback,
    closed: bool,
}

impl<P: Prompter> ClosingPrompter<P> {
    pub fn new(inner: P, fb: SharedFeedback) -> Self {
        ClosingPrompter { inner, fb, closed: false }
    }
}

impl<P: Prompter> Prompter for ClosingPrompter<P> {
    fn prompt(&mut self, available: Available) -> PromptResult {
        if !self.closed {
            self.closed = true;
            if self.fb.borrow().is_live() {
                // A beat so the last shake/✕ registers before the dialog.
                std::thread::sleep(std::time::Duration::from_millis(600));
                self.fb.borrow_mut().close();
            }
        }
        self.inner.prompt(available)
    }
}

/// Find `unlock_feedback.py` (same search order as the auth prompt):
/// `$APPLOCKER_FEEDBACK` → repo tree next to the exe → installed path → cwd.
fn locate_feedback_script() -> PathBuf {
    if let Ok(p) = std::env::var("APPLOCKER_FEEDBACK") {
        let path = PathBuf::from(p);
        if path.is_file() {
            return path;
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let repo = dir.join("../../../gui/unlock_feedback.py");
            if repo.is_file() {
                return repo;
            }
        }
    }
    let installed = PathBuf::from("/usr/lib/applocker/unlock_feedback.py");
    if installed.is_file() {
        return installed;
    }
    PathBuf::from("gui/unlock_feedback.py")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::{Method, NoFace};

    struct OkPrompt;
    impl Prompter for OkPrompt {
        fn prompt(&mut self, _a: Available) -> PromptResult {
            PromptResult::Entered { method: Method::Pin, secret: "x".into() }
        }
    }

    /// The window wrapper must never change a verdict — it only narrates.
    #[test]
    fn fallback_wrapper_passes_verdicts_through() {
        struct Fake {
            fp: Result<bool, String>,
        }
        impl Fallback for Fake {
            fn pin_available(&self) -> bool { true }
            fn sudo_available(&self) -> bool { false }
            fn verify_pin(&self, s: &str) -> bool { s == "1234" }
            fn verify_password(&self, _s: &str) -> Result<bool, String> { Ok(false) }
            fn fingerprint_available(&self) -> bool { true }
            fn verify_fingerprint(&self) -> Result<bool, String> { self.fp.clone() }
        }
        let none = || std::rc::Rc::new(std::cell::RefCell::new(Feedback::none()));

        let ok = FallbackWithFeedback::new(Fake { fp: Ok(true) }, none());
        assert_eq!(ok.verify_fingerprint(), Ok(true));
        assert!(ok.verify_pin("1234") && !ok.verify_pin("0000"));
        assert!(ok.pin_available() && !ok.sudo_available());

        let bad = FallbackWithFeedback::new(Fake { fp: Ok(false) }, none());
        assert_eq!(bad.verify_fingerprint(), Ok(false));

        let broken = FallbackWithFeedback::new(
            Fake { fp: Err("no reader".into()) },
            none(),
        );
        assert!(broken.verify_fingerprint().is_err(), "errors stay errors");
    }

    #[test]
    fn dead_feedback_is_harmless() {
        // With no child process, the whole decorator stack must behave exactly
        // like the undecorated verifier/prompter.
        let fb: SharedFeedback = Rc::new(RefCell::new(Feedback::none()));
        let mut face = FaceWithFeedback::new(Box::new(NoFace), fb.clone(), 3);
        assert!(!face.try_match());
        assert!(!face.try_match());
        let mut p = ClosingPrompter::new(OkPrompt, fb);
        assert!(matches!(
            p.prompt(Available { pin: true, sudo: true, fingerprint: false }),
            PromptResult::Entered { method: Method::Pin, .. }
        ));
    }

    #[test]
    fn attempt_counter_advances() {
        let fb: SharedFeedback = Rc::new(RefCell::new(Feedback::none()));
        let mut face = FaceWithFeedback::new(Box::new(NoFace), fb, 3);
        face.try_match();
        face.try_match();
        assert_eq!(face.attempt, 2);
    }
}
