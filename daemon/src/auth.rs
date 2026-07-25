//! The auth routine — one routine, used everywhere (README §"Auth routine").
//!
//! ```text
//! try face ─┐
//! try face  ├─ up to N×, 0.5s apart ── any match ─► ALLOW
//! try face ─┘
//!    └─ all fail ─► prompt PIN / sudo password ─► ALLOW / DENY
//! ```
//!
//! The routine is written against three tiny traits — a [`FaceVerifier`], a
//! [`Prompter`], and a [`Fallback`] — so the decision logic (attempt counts,
//! ret/cancel handling, which fallbacks are offered) is fully unit-testable with
//! fakes, no root/camera/display required. The real wiring lives in
//! [`SystemFallback`] (PIN + PAM) and the GUI-spawning prompter in `main.rs`.
//!
//! Invariant enforced here and echoed from the README: **at least one fallback
//! (PIN or sudo) is always available**, so a broken camera can never lock you
//! out of your own machine.

use std::path::PathBuf;
use std::thread;
use std::time::Duration;

use crate::pam;
use crate::pin;

/// Which fallback the user chose in the prompt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Method {
    Pin,
    Password,
}

/// What the prompter hands back.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PromptResult {
    Entered { method: Method, secret: String },
    Cancelled,
}

/// Final verdict of the routine.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Allowed,
    Denied,
}

/// Tier-1 recognition ("is this *me*?"). Stubbed until step 3 (face pipeline);
/// the stub always declines so we exercise the fallback path today.
pub trait FaceVerifier {
    /// One recognition attempt. `true` == matched the enrolled user.
    fn try_match(&mut self) -> bool;
}

/// Collects a secret from the user. The real impl spawns the GTK prompt; tests
/// use a scripted fake. `available` tells the prompt which methods to offer.
pub trait Prompter {
    fn prompt(&mut self, available: Available) -> PromptResult;
}

/// Verifies a fallback secret. Split from the prompt so the accept/reject
/// decision stays in the (root) daemon, never in the GUI.
pub trait Fallback {
    fn pin_available(&self) -> bool;
    fn sudo_available(&self) -> bool;
    fn verify_pin(&self, secret: &str) -> bool;
    /// `Err` means PAM is unavailable — treated as reject, never as accept.
    fn verify_password(&self, secret: &str) -> Result<bool, String>;
}

/// Which fallbacks to present in the prompt this time.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Available {
    pub pin: bool,
    pub sudo: bool,
}

/// Tunables for the routine.
#[derive(Debug, Clone)]
pub struct Config {
    /// Face recognition attempts before falling back (README: up to 3).
    pub face_attempts: u32,
    /// Delay between face attempts.
    pub face_gap: Duration,
    /// How many wrong PIN/password entries before we give up (Cancel always
    /// gives up immediately).
    pub fallback_attempts: u32,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            face_attempts: 3,
            face_gap: Duration::from_millis(500),
            fallback_attempts: 3,
        }
    }
}

/// Run the full routine. Returns [`Outcome::Allowed`] the moment any factor
/// succeeds, [`Outcome::Denied`] if the user cancels or exhausts fallback tries.
///
/// Takes trait objects so the binary can pick face/fallback implementations at
/// runtime (NoFace vs the subprocess recogniser) without monomorphising every
/// call site.
pub fn run(
    cfg: &Config,
    face: &mut dyn FaceVerifier,
    prompter: &mut dyn Prompter,
    fallback: &dyn Fallback,
) -> Outcome {
    // Tier 1: recognition, up to N attempts.
    for attempt in 0..cfg.face_attempts {
        if face.try_match() {
            return Outcome::Allowed;
        }
        if attempt + 1 < cfg.face_attempts {
            thread::sleep(cfg.face_gap);
        }
    }

    // Tier 2: fallback. The always-one-fallback invariant: if neither is
    // configured we must not silently deny — that would be a lock-out. Treat it
    // as a misconfiguration and deny loudly (the installer guarantees a PIN, so
    // this only fires if the file was deleted out from under us).
    let available = Available {
        pin: fallback.pin_available(),
        sudo: fallback.sudo_available(),
    };
    if !available.pin && !available.sudo {
        eprintln!(
            "applockerd: no fallback available (no PIN set and PAM unusable) — denying. \
             This is a misconfiguration; see recovery in README."
        );
        return Outcome::Denied;
    }

    for _ in 0..cfg.fallback_attempts {
        match prompter.prompt(available) {
            PromptResult::Cancelled => return Outcome::Denied,
            PromptResult::Entered { method, secret } => {
                let mut secret = secret;
                let ok = match method {
                    Method::Pin => available.pin && fallback.verify_pin(&secret),
                    Method::Password => {
                        available.sudo
                            && fallback.verify_password(&secret).unwrap_or_else(|e| {
                                eprintln!("applockerd: PAM error, treating as reject: {e}");
                                false
                            })
                    }
                };
                // Best-effort: don't leave the secret sitting in freed memory.
                // (NUL bytes are valid UTF-8, so this is safe.)
                unsafe { secret.as_bytes_mut().fill(0) };
                if ok {
                    return Outcome::Allowed;
                }
            }
        }
    }
    Outcome::Denied
}

// ── Real implementations ─────────────────────────────────────────────────────

/// A face verifier that always declines — placeholder until step 3 wires in the
/// OpenCV/MediaPipe pipeline. With this in place the routine goes straight to
/// the PIN/sudo prompt, which is exactly what we want to build and test now.
pub struct NoFace;
impl FaceVerifier for NoFace {
    fn try_match(&mut self) -> bool {
        false
    }
}

/// Whose password the sudo fallback should check. `SUDO_USER` when we were
/// launched via sudo; otherwise, when we run as the root service, the active
/// desktop user (checking *root's* password would be wrong — the person at the
/// keyboard is the desktop user). Falls back to `root` only when headless.
fn desktop_user() -> String {
    if let Some(u) = pam::invoking_user() {
        if u != "root" {
            return u;
        }
    }
    if let Some(ctx) = crate::session::SessionCtx::discover() {
        if ctx.uid != 0 {
            return ctx.user;
        }
    }
    "root".to_string()
}

/// The PIN record location: `$APPLOCKER_PIN_FILE` if set, else the system path.
pub fn default_pin_path() -> PathBuf {
    match std::env::var_os("APPLOCKER_PIN_FILE") {
        Some(p) => PathBuf::from(p),
        None => PathBuf::from("/etc/applocker/pin"),
    }
}

/// Real fallback: PIN via `pin.rs`, sudo password via `pam.rs`, each gated by
/// the user's [`Policy`](crate::policy::Policy).
pub struct SystemFallback {
    pub pin_path: PathBuf,
    pub pam_service: String,
    pub user: String,
    /// Policy switch: offer the PIN fallback at all.
    pub allow_pin: bool,
    /// Policy switch: offer the sudo-password fallback at all.
    pub allow_sudo: bool,
}

impl SystemFallback {
    /// Default locations for the deployed daemon, gated by the saved policy.
    /// `$APPLOCKER_PIN_FILE` overrides the PIN path (used by tests and by
    /// `set-pin`/`auth-test` so they don't need to touch `/etc`).
    pub fn system() -> SystemFallback {
        let policy = crate::policy::load_default();
        SystemFallback {
            pin_path: default_pin_path(),
            pam_service: "sudo".to_string(),
            user: desktop_user(),
            allow_pin: policy.allow_pin,
            allow_sudo: policy.allow_sudo,
        }
    }
}

impl Fallback for SystemFallback {
    fn pin_available(&self) -> bool {
        self.allow_pin && pin::is_set(&self.pin_path)
    }

    fn sudo_available(&self) -> bool {
        // Gated by policy; otherwise we can't cheaply prove PAM will accept
        // anything, but a resolved user name means the sudo path is usable
        // (PAM itself is loaded lazily at verify time).
        self.allow_sudo && !self.user.is_empty()
    }

    fn verify_pin(&self, secret: &str) -> bool {
        pin::verify_pin(&self.pin_path, secret).unwrap_or(false)
    }

    fn verify_password(&self, secret: &str) -> Result<bool, String> {
        pam::verify_password(&self.pam_service, &self.user, secret)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct ScriptedFace {
        results: Vec<bool>,
        idx: usize,
    }
    impl FaceVerifier for ScriptedFace {
        fn try_match(&mut self) -> bool {
            let r = self.results.get(self.idx).copied().unwrap_or(false);
            self.idx += 1;
            r
        }
    }

    struct ScriptedPrompt {
        answers: Vec<PromptResult>,
        idx: usize,
        seen_available: Vec<Available>,
    }
    impl Prompter for ScriptedPrompt {
        fn prompt(&mut self, available: Available) -> PromptResult {
            self.seen_available.push(available);
            let r = self
                .answers
                .get(self.idx)
                .cloned()
                .unwrap_or(PromptResult::Cancelled);
            self.idx += 1;
            r
        }
    }

    struct FakeFallback {
        pin: bool,
        sudo: bool,
        good_pin: &'static str,
        good_pw: &'static str,
    }
    impl Fallback for FakeFallback {
        fn pin_available(&self) -> bool {
            self.pin
        }
        fn sudo_available(&self) -> bool {
            self.sudo
        }
        fn verify_pin(&self, secret: &str) -> bool {
            secret == self.good_pin
        }
        fn verify_password(&self, secret: &str) -> Result<bool, String> {
            Ok(secret == self.good_pw)
        }
    }

    fn fast_cfg() -> Config {
        Config {
            face_attempts: 3,
            face_gap: Duration::from_millis(0),
            fallback_attempts: 3,
        }
    }

    fn entered(method: Method, s: &str) -> PromptResult {
        PromptResult::Entered {
            method,
            secret: s.to_string(),
        }
    }

    #[test]
    fn face_match_short_circuits() {
        let mut face = ScriptedFace {
            results: vec![false, true],
            idx: 0,
        };
        let mut prompt = ScriptedPrompt {
            answers: vec![],
            idx: 0,
            seen_available: vec![],
        };
        let fb = FakeFallback {
            pin: true,
            sudo: true,
            good_pin: "1234",
            good_pw: "pw",
        };
        assert_eq!(run(&fast_cfg(), &mut face, &mut prompt, &fb), Outcome::Allowed);
        // Never prompted, since face matched on attempt 2.
        assert!(prompt.seen_available.is_empty());
    }

    #[test]
    fn correct_pin_allows_after_face_fails() {
        let mut face = ScriptedFace {
            results: vec![false, false, false],
            idx: 0,
        };
        let mut prompt = ScriptedPrompt {
            answers: vec![entered(Method::Pin, "1234")],
            idx: 0,
            seen_available: vec![],
        };
        let fb = FakeFallback {
            pin: true,
            sudo: true,
            good_pin: "1234",
            good_pw: "pw",
        };
        assert_eq!(run(&fast_cfg(), &mut face, &mut prompt, &fb), Outcome::Allowed);
        assert_eq!(prompt.seen_available[0], Available { pin: true, sudo: true });
    }

    #[test]
    fn wrong_then_right_within_retries() {
        let mut face = ScriptedFace { results: vec![false; 3], idx: 0 };
        let mut prompt = ScriptedPrompt {
            answers: vec![
                entered(Method::Pin, "0000"),
                entered(Method::Password, "nope"),
                entered(Method::Pin, "1234"),
            ],
            idx: 0,
            seen_available: vec![],
        };
        let fb = FakeFallback { pin: true, sudo: true, good_pin: "1234", good_pw: "pw" };
        assert_eq!(run(&fast_cfg(), &mut face, &mut prompt, &fb), Outcome::Allowed);
    }

    #[test]
    fn cancel_denies_immediately() {
        let mut face = ScriptedFace { results: vec![false; 3], idx: 0 };
        let mut prompt = ScriptedPrompt {
            answers: vec![PromptResult::Cancelled, entered(Method::Pin, "1234")],
            idx: 0,
            seen_available: vec![],
        };
        let fb = FakeFallback { pin: true, sudo: true, good_pin: "1234", good_pw: "pw" };
        assert_eq!(run(&fast_cfg(), &mut face, &mut prompt, &fb), Outcome::Denied);
        // Only one prompt shown; the second scripted answer is never reached.
        assert_eq!(prompt.seen_available.len(), 1);
    }

    #[test]
    fn exhausting_retries_denies() {
        let mut face = ScriptedFace { results: vec![false; 3], idx: 0 };
        let mut prompt = ScriptedPrompt {
            answers: vec![
                entered(Method::Pin, "a"),
                entered(Method::Pin, "b"),
                entered(Method::Pin, "c"),
                entered(Method::Pin, "1234"), // would work, but we're out of tries
            ],
            idx: 0,
            seen_available: vec![],
        };
        let fb = FakeFallback { pin: true, sudo: true, good_pin: "1234", good_pw: "pw" };
        assert_eq!(run(&fast_cfg(), &mut face, &mut prompt, &fb), Outcome::Denied);
        assert_eq!(prompt.seen_available.len(), 3);
    }

    #[test]
    fn no_fallback_available_denies_without_prompting() {
        let mut face = ScriptedFace { results: vec![false; 3], idx: 0 };
        let mut prompt = ScriptedPrompt { answers: vec![], idx: 0, seen_available: vec![] };
        let fb = FakeFallback { pin: false, sudo: false, good_pin: "x", good_pw: "y" };
        assert_eq!(run(&fast_cfg(), &mut face, &mut prompt, &fb), Outcome::Denied);
        assert!(prompt.seen_available.is_empty());
    }

    #[test]
    fn pin_rejected_when_pin_not_available() {
        // User somehow submits a PIN but only sudo is offered — must not accept.
        let mut face = ScriptedFace { results: vec![false; 3], idx: 0 };
        let mut prompt = ScriptedPrompt {
            answers: vec![entered(Method::Pin, "1234"), PromptResult::Cancelled],
            idx: 0,
            seen_available: vec![],
        };
        let fb = FakeFallback { pin: false, sudo: true, good_pin: "1234", good_pw: "pw" };
        assert_eq!(run(&fast_cfg(), &mut face, &mut prompt, &fb), Outcome::Denied);
        assert_eq!(prompt.seen_available[0], Available { pin: false, sudo: true });
    }
}
