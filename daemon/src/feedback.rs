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
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::rc::Rc;

use crate::auth::{Available, FaceVerifier, Prompter, PromptResult};

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

    pub fn fail(&mut self, attempt: u32, total: u32) {
        self.send(&format!("fail {attempt} {total}"));
    }

    /// Show the green check; the window closes itself shortly after.
    pub fn ok(&mut self) {
        self.send("ok");
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
            p.prompt(Available { pin: true, sudo: true }),
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
