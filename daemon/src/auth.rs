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
    /// Fingerprint reader, verified by PAM (`pam_fprintd`). Carries no secret:
    /// the prompt just says "use the reader" and the daemon runs the PAM stack.
    Fingerprint,
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
    /// Is a fingerprint reader usable *and* a finger enrolled for this user?
    fn fingerprint_available(&self) -> bool {
        false
    }
    /// Blocks while the reader waits for a touch. `Err` = unusable, never accept.
    fn verify_fingerprint(&self) -> Result<bool, String> {
        Err("fingerprint not supported".to_string())
    }
}

/// Which fallbacks to present in the prompt this time.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Available {
    pub pin: bool,
    pub sudo: bool,
    pub fingerprint: bool,
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
    // Tier 0: the fingerprint reader, when a finger is enrolled. It goes first
    // because it's the fastest factor and needs no camera — touch the reader
    // and you're in, with no button to press first. Ignore it and it falls
    // through to face, then the prompt (where it's offered again as a button).
    if fallback.fingerprint_available() {
        match fallback.verify_fingerprint() {
            Ok(true) => return Outcome::Allowed,
            Ok(false) => {}
            Err(e) => eprintln!("applockerd: fingerprint unusable: {e}"),
        }
    }

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
        fingerprint: fallback.fingerprint_available(),
    };
    if !available.pin && !available.sudo && !available.fingerprint {
        eprintln!(
            "applockerd: no fallback available (no PIN set and PAM unusable) — denying. \
             This is a misconfiguration; see recovery in README."
        );
        return Outcome::Denied;
    }

    // Attempts are a brute-force budget, so only *guessable* answers spend one.
    // A fingerprint can't be guessed, and on flaky reader hardware (libfprint
    // driver errors) it fails for reasons that have nothing to do with the
    // person — eating a password try for that is just punishing the user.
    let mut tries_left = cfg.fallback_attempts;
    while tries_left > 0 {
        match prompter.prompt(available) {
            PromptResult::Cancelled => return Outcome::Denied,
            PromptResult::Entered { method, secret } => {
                if method != Method::Fingerprint {
                    tries_left -= 1;
                }
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
                    Method::Fingerprint => {
                        available.fingerprint
                            && fallback.verify_fingerprint().unwrap_or_else(|e| {
                                eprintln!("applockerd: fprintd error, treating as reject: {e}");
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
    /// Policy switch: try the fingerprint reader at all.
    pub allow_fingerprint: bool,
}

/// PAM service that runs `pam_fprintd` alone (shipped as /etc/pam.d/ by the
/// package). Separate from `sudo` so a fingerprint touch can never be answered
/// by a password, and a missing fprintd can't weaken the sudo stack.
pub const FPRINTD_SERVICE: &str = "applocker-fprintd";

/// PAM service for checking a *typed* password. Ours when the package shipped
/// it (pam_unix only — see the file's comment), else the distro's `sudo` stack.
pub const PASSWORD_SERVICE: &str = "applocker-password";

fn password_service() -> String {
    if std::path::Path::new("/etc/pam.d/applocker-password").is_file() {
        PASSWORD_SERVICE.to_string()
    } else {
        "sudo".to_string()
    }
}

/// Where an accepted fingerprint is stamped. A touch is spoof-resistant, so for
/// a while afterwards the lock screen accepts a plain glance instead of the turn
/// challenge (`face/watch_presence.py` reads this). On tmpfs on purpose: a
/// reboot must not carry the trust over. World-readable, mtime is the whole
/// payload — it says "a finger was verified at <time>", nothing about whose.
pub const FP_STAMP: &str = "/run/applocker/last-fingerprint";

/// libfprint's own words when a driver claims a match it cannot justify. When
/// this is in fprintd's log for the attempt we just made, the PAM success is
/// not evidence of anything and must not open a lock.
const BOGUS_MATCH_MARKERS: [&str; 2] = [
    "not matching any in the gallery",
    "fp_print_equal", // "should not be reached" — same event, different line
];

/// Pure half of the veto, so it can be tested without a reader.
fn log_disowns_the_match(log: &str) -> bool {
    BOGUS_MATCH_MARKERS.iter().any(|m| log.contains(m))
}

/// Did fprintd disown a match in the last minute? Root-only read, bounded.
fn fprintd_disowned_recent_match() -> bool {
    match std::process::Command::new("journalctl")
        .args(["-u", "fprintd", "--no-pager", "-S", "-1min"])
        .output()
    {
        Ok(o) => log_disowns_the_match(&String::from_utf8_lossy(&o.stdout)),
        // Can't read the log → can't veto. Say so rather than silently trusting.
        Err(e) => {
            eprintln!("applockerd: cannot check fprintd's log ({e}); \
                       accepting the reader's word for this attempt");
            false
        }
    }
}

fn stamp_fingerprint() {
    let dir = std::path::Path::new(FP_STAMP).parent();
    if let Some(d) = dir {
        let _ = std::fs::create_dir_all(d);
    }
    if let Err(e) = std::fs::write(FP_STAMP, b"") {
        eprintln!("applockerd: could not stamp {FP_STAMP}: {e}");
    }
}

/// Has `user` enrolled at least one finger with fprintd? `fprintd-list` prints
/// one `- #0: <finger>` line per enrolled finger and exits non-zero when there
/// is no reader — exactly the "don't offer it" condition.
///
/// Wrapped in `timeout` because an unprivileged `fprintd-list` waits on a
/// polkit prompt that nobody will answer (~25s). We always run as root here, so
/// that shouldn't happen, but a stalled probe would delay the unlock prompt.
fn fingerprint_enrolled(user: &str) -> bool {
    // On by default again (`APPLOCKER_FP=0` disables), because the bypass this
    // tier produced on 2026-11-24 now has a specific guard rather than a blanket
    // ban: `verify_fingerprint` throws away any PAM success that fprintd's own
    // log disowns ("not matching any in the gallery"). That was exactly the
    // failure — three allows with no finger on the reader.
    //
    // The three behaviours this gives, which is what was asked for:
    //   reader absent / unenrolled  → tier skipped, straight to face or PIN
    //   reader back                 → used again at the next unlock, no restart
    //   reader lying                → success vetoed, falls back to face / PIN
    //
    // Note what it does NOT cover: the system's own fingerprint PAM (sudo, the
    // KDE greeter) has no such veto and cannot get one from here.
    if std::env::var("APPLOCKER_FP").map(|v| v == "0").unwrap_or(false) {
        return false;
    }
    if !std::path::Path::new("/etc/pam.d/applocker-fprintd").is_file() {
        return false;
    }
    match std::process::Command::new("timeout")
        .args(["3", "fprintd-list", user])
        .output()
    {
        Ok(o) => o.status.success() && String::from_utf8_lossy(&o.stdout).contains("- #"),
        Err(_) => false,
    }
}

impl SystemFallback {
    /// Default locations for the deployed daemon, gated by the saved policy.
    /// `$APPLOCKER_PIN_FILE` overrides the PIN path (used by tests and by
    /// `set-pin`/`auth-test` so they don't need to touch `/etc`).
    pub fn system() -> SystemFallback {
        let policy = crate::policy::load_default();
        SystemFallback {
            pin_path: default_pin_path(),
            pam_service: password_service(),
            user: desktop_user(),
            allow_pin: policy.allow_pin,
            allow_sudo: policy.allow_sudo,
            allow_fingerprint: policy.allow_fingerprint,
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

    fn fingerprint_available(&self) -> bool {
        self.allow_fingerprint && fingerprint_enrolled(&self.user)
    }

    /// pam_fprintd asks nothing of the conversation (it only sends info
    /// messages, which `pam.rs` answers with NULL), so the same PAM path works
    /// with an empty secret. Blocks until the user touches the reader or the
    /// module times out.
    fn verify_fingerprint(&self) -> Result<bool, String> {
        let ok = pam::verify_password(FPRINTD_SERVICE, &self.user, "")?;
        // A PAM success is not the end of it. On 2026-11-24 this reader produced
        // three successes with no finger on it, while libfprint logged "Driver
        // reported a match providing a scanned print that is not matching any in
        // the gallery" at the same seconds. libfprint knows the match was
        // rubbish; PAM just doesn't pass that on. So we ask the log.
        if ok && fprintd_disowned_recent_match() {
            eprintln!(
                "applockerd: REJECTING a fingerprint success — fprintd reports the \
                 driver's match was not in the gallery (flaky reader). Falling \
                 back to face / PIN."
            );
            return Ok(false);
        }
        if ok {
            stamp_fingerprint();
        }
        Ok(ok)
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

    /// Fallback with a reader attached; `touched` decides what it reports.
    struct FpFallback {
        touched: bool,
    }
    impl Fallback for FpFallback {
        fn pin_available(&self) -> bool {
            true
        }
        fn sudo_available(&self) -> bool {
            false
        }
        fn verify_pin(&self, secret: &str) -> bool {
            secret == "1234"
        }
        fn verify_password(&self, _secret: &str) -> Result<bool, String> {
            Ok(false)
        }
        fn fingerprint_available(&self) -> bool {
            true
        }
        fn verify_fingerprint(&self) -> Result<bool, String> {
            Ok(self.touched)
        }
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

    /// Touching the reader gets you in with no dialog and no camera — that's
    /// the "no button" path.
    #[test]
    fn fingerprint_allows_before_face_or_prompt() {
        let mut face = ScriptedFace { results: vec![true], idx: 0 };
        let mut prompt = ScriptedPrompt { answers: vec![], idx: 0, seen_available: vec![] };
        assert_eq!(
            run(&fast_cfg(), &mut face, &mut prompt, &FpFallback { touched: true }),
            Outcome::Allowed
        );
        assert_eq!(face.idx, 0, "face must not run once the reader accepted");
        assert!(prompt.seen_available.is_empty());
    }

    /// The veto that makes a flaky reader survivable: libfprint saying the
    /// driver's match wasn't in the gallery turns a PAM success into a failure.
    #[test]
    fn a_disowned_match_is_not_a_match() {
        let bogus = "fprintd[123]: Driver reported a match providing a scanned \
                     print that is not matching any in the gallery.";
        assert!(log_disowns_the_match(bogus));
        assert!(log_disowns_the_match(
            "file ../libfprint/fp-print.c: line 636 (fp_print_equal): should not be reached"
        ));
        // Ordinary noise, including a plain disconnect, must NOT veto a success.
        assert!(!log_disowns_the_match("Read failed: device was disconnected, aborting"));
        assert!(!log_disowns_the_match("Started fprintd.service"));
        assert!(!log_disowns_the_match(""));
    }

    /// A failing reader must not burn the password-attempt budget: the user
    /// still gets all three typed tries after the finger fails.
    #[test]
    fn failed_fingerprint_does_not_spend_a_password_try() {
        let mut face = ScriptedFace { results: vec![false; 3], idx: 0 };
        let mut prompt = ScriptedPrompt {
            answers: vec![
                entered(Method::Fingerprint, ""), // reader error / wrong finger
                entered(Method::Fingerprint, ""), // again
                entered(Method::Pin, "0000"),     // wrong  (try 1)
                entered(Method::Pin, "9999"),     // wrong  (try 2)
                entered(Method::Pin, "1234"),     // right  (try 3)
            ],
            idx: 0,
            seen_available: vec![],
        };
        assert_eq!(
            run(&fast_cfg(), &mut face, &mut prompt, &FpFallback { touched: false }),
            Outcome::Allowed
        );
        assert_eq!(prompt.seen_available.len(), 5, "all five prompts shown");
    }

    /// Ignoring the reader must not deny — it falls through to face, then the
    /// prompt, which still offers the reader as a button.
    #[test]
    fn unused_fingerprint_falls_through_to_the_prompt() {
        let mut face = ScriptedFace { results: vec![false; 3], idx: 0 };
        let mut prompt = ScriptedPrompt {
            answers: vec![entered(Method::Pin, "1234")],
            idx: 0,
            seen_available: vec![],
        };
        assert_eq!(
            run(&fast_cfg(), &mut face, &mut prompt, &FpFallback { touched: false }),
            Outcome::Allowed
        );
        assert_eq!(prompt.seen_available[0].fingerprint, true);
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
        assert_eq!(prompt.seen_available[0], Available { pin: true, sudo: true, fingerprint: false });
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
        assert_eq!(prompt.seen_available[0], Available { pin: false, sudo: true, fingerprint: false });
    }
}
