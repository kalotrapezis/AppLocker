//! Glue between the fanotify exec-gate and the auth routine: the GUI-spawning
//! prompter, the in-memory unlock cache, and helpers to locate the prompt.
//!
//! None of this is the *policy* (that's `auth.rs`); this is the deployment-side
//! plumbing that policy is too pure to know about.

use std::collections::HashSet;
use std::io::Read;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::Mutex;

use crate::auth::{Available, Method, Prompter, PromptResult};

/// Remembers which locked targets are currently unlocked, and which have a
/// prompt already on screen (so a second exec of the same app doesn't stack a
/// second dialog).
///
/// For this step the cache lives only in memory, so it's implicitly wiped on
/// daemon restart. Step 5 wires it to logind Lock/Unlock and the attention
/// watcher for the real "wipe on lock/reboot" behaviour.
#[derive(Default)]
pub struct UnlockCache {
    inner: Mutex<Inner>,
}

#[derive(Default)]
struct Inner {
    unlocked: HashSet<String>,
    pending: HashSet<String>,
}

/// How long an unlock lasts for a given app.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CachePolicy {
    /// Authenticate once, then don't ask again until the next session lock
    /// (manual, lid-close, or attention-lock — all wipe the cache).
    OncePerSession,
    /// Re-authenticate on every launch; never cached. For your most sensitive
    /// apps, or when auth is fast enough (face) that re-asking is painless.
    EveryTime,
}

/// Outcome of asking the cache what to do with a locked exec right now.
pub enum Decision {
    /// Already unlocked — allow without prompting.
    AlreadyUnlocked,
    /// A prompt is already open for this target — don't stack another.
    PromptInFlight,
    /// Caller should now run the auth routine (we've marked it pending).
    NeedsAuth,
}

impl UnlockCache {
    pub fn new() -> UnlockCache {
        UnlockCache::default()
    }

    /// Decide what to do with a fresh locked exec of `target`, reserving a
    /// pending slot when auth is needed. `EveryTime` apps are never served from
    /// the unlocked set — they always re-auth — but still use the pending slot
    /// so two simultaneous launches don't stack two prompts.
    pub fn begin(&self, target: &str, policy: CachePolicy) -> Decision {
        let mut inner = self.inner.lock().unwrap();
        if policy == CachePolicy::OncePerSession && inner.unlocked.contains(target) {
            Decision::AlreadyUnlocked
        } else if inner.pending.contains(target) {
            Decision::PromptInFlight
        } else {
            inner.pending.insert(target.to_string());
            Decision::NeedsAuth
        }
    }

    /// Auth for `target` finished. Under `OncePerSession`, an allowed result is
    /// remembered until the next `wipe()`; under `EveryTime` it's never cached.
    pub fn finish(&self, target: &str, allowed: bool, policy: CachePolicy) {
        let mut inner = self.inner.lock().unwrap();
        inner.pending.remove(target);
        if allowed && policy == CachePolicy::OncePerSession {
            inner.unlocked.insert(target.to_string());
        }
    }

    /// Wipe every unlock (called on session-lock / reboot once that's wired).
    #[allow(dead_code)]
    pub fn wipe(&self) {
        let mut inner = self.inner.lock().unwrap();
        inner.unlocked.clear();
    }
}

/// Spawns the GTK auth prompt and parses its one-line reply. Reused across the
/// retry loop inside a single auth run, so it tracks whether a previous attempt
/// failed and shows that hint on the next dialog.
pub struct GuiPrompter {
    app_name: String,
    script: PathBuf,
    attempt: u32,
    /// Whether a real face attempt ran before we fell back to this prompt. Only
    /// then should the dialog say "Face not recognised"; with face off / not
    /// enrolled it would be a lie (see gui/auth_prompt.py).
    face_was_live: bool,
}

impl GuiPrompter {
    pub fn new(app_name: &str, face_was_live: bool) -> GuiPrompter {
        GuiPrompter {
            app_name: app_name.to_string(),
            script: locate_prompt_script(),
            attempt: 0,
            face_was_live,
        }
    }
}

impl Prompter for GuiPrompter {
    fn prompt(&mut self, available: Available) -> PromptResult {
        let mut methods = Vec::new();
        if available.pin {
            methods.push("pin");
        }
        if available.sudo {
            methods.push("sudo");
        }

        let mut cmd = Command::new("python3");
        cmd.arg(&self.script)
            .arg("--app")
            .arg(&self.app_name)
            .arg("--methods")
            .arg(methods.join(","));
        if self.attempt > 0 {
            cmd.arg("--error").arg("Incorrect — try again.");
        }
        if self.face_was_live {
            cmd.arg("--face-failed");
        }
        self.attempt += 1;

        // Secret comes back on stdout; keep stderr for our own logging.
        cmd.stdout(Stdio::piped()).stderr(Stdio::inherit());

        // When we're the root service, show the prompt in the user's session.
        crate::session::attach(&mut cmd);

        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                eprintln!("applockerd: failed to launch auth prompt: {e}");
                return PromptResult::Cancelled;
            }
        };

        let mut out = String::new();
        if let Some(mut stdout) = child.stdout.take() {
            let _ = stdout.read_to_string(&mut out);
        }
        let _ = child.wait();

        parse_reply(&out)
    }
}

/// Parse the prompt's stdout contract (see gui/auth_prompt.py):
/// `cancel`, `pin\t<secret>`, or `sudo\t<secret>`.
fn parse_reply(out: &str) -> PromptResult {
    let line = out.lines().next().unwrap_or("").trim_end_matches('\n');
    if line == "cancel" || line.is_empty() {
        return PromptResult::Cancelled;
    }
    if let Some((tag, secret)) = line.split_once('\t') {
        let method = match tag {
            "pin" => Method::Pin,
            "sudo" => Method::Password,
            _ => return PromptResult::Cancelled,
        };
        return PromptResult::Entered {
            method,
            secret: secret.to_string(),
        };
    }
    PromptResult::Cancelled
}

/// Find `auth_prompt.py`:
///   1. `$APPLOCKER_AUTH_PROMPT` if it points at a real file,
///   2. `<exe_dir>/../../../gui/auth_prompt.py` (running from the repo tree),
///   3. `/usr/lib/applocker/auth_prompt.py` (installed layout),
///   4. `gui/auth_prompt.py` relative to the cwd (last resort).
fn locate_prompt_script() -> PathBuf {
    if let Ok(p) = std::env::var("APPLOCKER_AUTH_PROMPT") {
        let path = PathBuf::from(p);
        if path.is_file() {
            return path;
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        // exe = .../daemon/target/release/applockerd
        if let Some(dir) = exe.parent() {
            let repo = dir.join("../../../gui/auth_prompt.py");
            if repo.is_file() {
                return repo;
            }
        }
    }
    let installed = PathBuf::from("/usr/lib/applocker/auth_prompt.py");
    if installed.is_file() {
        return installed;
    }
    PathBuf::from("gui/auth_prompt.py")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cache_lifecycle_once_per_session() {
        let p = CachePolicy::OncePerSession;
        let cache = UnlockCache::new();
        // First exec needs auth and reserves a pending slot.
        assert!(matches!(cache.begin("calc", p), Decision::NeedsAuth));
        // Second, while pending, must not stack a prompt.
        assert!(matches!(cache.begin("calc", p), Decision::PromptInFlight));
        // Deny: clears pending, not unlocked.
        cache.finish("calc", false, p);
        assert!(matches!(cache.begin("calc", p), Decision::NeedsAuth));
        // Allow: now cached.
        cache.finish("calc", true, p);
        assert!(matches!(cache.begin("calc", p), Decision::AlreadyUnlocked));
        // Wipe returns it to needing auth.
        cache.wipe();
        assert!(matches!(cache.begin("calc", p), Decision::NeedsAuth));
    }

    #[test]
    fn cache_every_time_never_caches() {
        let p = CachePolicy::EveryTime;
        let cache = UnlockCache::new();
        assert!(matches!(cache.begin("bank", p), Decision::NeedsAuth));
        // Simultaneous second launch still won't stack a prompt.
        assert!(matches!(cache.begin("bank", p), Decision::PromptInFlight));
        // Even after a successful auth, the next launch re-authenticates.
        cache.finish("bank", true, p);
        assert!(matches!(cache.begin("bank", p), Decision::NeedsAuth));
    }

    #[test]
    fn parse_reply_cases() {
        assert_eq!(parse_reply("cancel\n"), PromptResult::Cancelled);
        assert_eq!(parse_reply(""), PromptResult::Cancelled);
        assert_eq!(
            parse_reply("pin\t1234\n"),
            PromptResult::Entered { method: Method::Pin, secret: "1234".into() }
        );
        assert_eq!(
            parse_reply("sudo\thunter2\n"),
            PromptResult::Entered { method: Method::Password, secret: "hunter2".into() }
        );
        // Secret may contain spaces; only the first tab splits.
        assert_eq!(
            parse_reply("sudo\tpass word\n"),
            PromptResult::Entered { method: Method::Password, secret: "pass word".into() }
        );
        assert_eq!(parse_reply("garbage"), PromptResult::Cancelled);
    }
}
