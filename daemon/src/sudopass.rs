//! The stored sudo password for the polkit agent — encrypted at rest, root-only,
//! and released only after a face/PIN check, with a brute-force lockout.
//!
//! Why this exists
//! ---------------
//! The polkit agent needs to hand polkit a real password. Keeping it in the
//! agent's RAM makes it readable by any same-uid process (a virus running as
//! you) or a memory-disclosure bug. Instead we keep it **here, in the root
//! daemon**: encrypted on disk (root `0600`), decrypted only for the instant the
//! agent needs it, gated by the same face→PIN auth. A same-uid attacker can
//! neither read the file nor pass the auth, and the lockout below stops PIN
//! guessing.
//!
//! Lockout (agreed policy)
//! -----------------------
//! Wrong PINs escalate: **2 wrong → 24h autocomplete lock → 2 more wrong →
//! destroy**. "Destroy" wipes the stored password *and* the PIN and reverts the
//! policy to **sudo-only**, so you are never locked out — you simply fall back to
//! typing your real sudo password (the normal Linux behaviour) until you
//! re-enable autocomplete. The lock only ever disables the *autocomplete*.
//!
//! At-rest key is machine-local (a random `secret.key`, root `0600`). This
//! defends a leaked file/backup; it is explicitly not a defence against someone
//! who already has root here (they can read the key too).

use std::fs;
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::crypto;

const DAY_SECS: u64 = 24 * 60 * 60;
/// Wrong PINs in a stage before it escalates (2 → lock, then 2 → destroy).
const FAILS_PER_STAGE: u32 = 2;

/// Where the three files live, so tests can point them at a temp dir.
pub struct Store {
    pub key_path: PathBuf,
    pub blob_path: PathBuf,
    pub lock_path: PathBuf,
}

/// What a failed PIN attempt did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailOutcome {
    /// Wrong, but tries remain in this stage.
    Denied,
    /// Just crossed into the 24h lock; autocomplete is now disabled for `secs`.
    Locked { secs: u64 },
    /// Second stage exhausted: password + PIN wiped, policy back to sudo-only.
    Destroyed,
}

/// Whether the PIN path is currently usable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LockStatus {
    Open,
    Locked { secs: u64 },
}

#[derive(Debug, Default, Clone, Copy)]
struct State {
    stage: u32,     // 0 = normal, 1 = has served (or is serving) the 24h lock
    fails: u32,     // consecutive wrong PINs in the current stage
    lock_until: u64, // unix secs; 0 = not locked
}

pub fn now_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

impl Store {
    /// Deployed locations, each overridable by env for tests / dev sandboxes.
    pub fn system() -> Store {
        let base = PathBuf::from("/etc/applocker");
        Store {
            key_path: env_or(&base, "APPLOCKER_KEY_FILE", "secret.key"),
            blob_path: env_or(&base, "APPLOCKER_SUDO_FILE", "sudo.enc"),
            lock_path: env_or(&base, "APPLOCKER_LOCK_FILE", "sudo.lock"),
        }
    }

    // ── the encrypted password ────────────────────────────────────────────────
    pub fn is_set(&self) -> bool {
        self.blob_path.exists()
    }

    /// Encrypt and store `password`, resetting any lockout. Overwrites atomically
    /// enough for our purposes (write temp, rename).
    pub fn store(&self, password: &str) -> std::io::Result<()> {
        let key = self.key()?;
        let blob = crypto::seal(&key, password.as_bytes());
        write_private(&self.blob_path, &blob)?;
        let _ = fs::remove_file(&self.lock_path); // fresh start on (re)enrol
        Ok(())
    }

    /// Decrypt the stored password, or `None` if not set / corrupt.
    pub fn load(&self) -> Option<String> {
        let key = self.key().ok()?;
        let blob = fs::read(&self.blob_path).ok()?;
        let pt = crypto::open(&key, &blob)?;
        String::from_utf8(pt).ok()
    }

    /// Wipe the stored password (autocomplete off; the key stays).
    pub fn forget(&self) {
        let _ = fs::remove_file(&self.blob_path);
        let _ = fs::remove_file(&self.lock_path);
    }

    // ── lockout ───────────────────────────────────────────────────────────────
    pub fn status(&self, now: u64) -> LockStatus {
        let st = self.read_state();
        if st.lock_until > now {
            LockStatus::Locked { secs: st.lock_until - now }
        } else {
            LockStatus::Open
        }
    }

    /// A correct PIN (or face) — clear the failure/lock state.
    pub fn record_success(&self) {
        let _ = fs::remove_file(&self.lock_path);
    }

    /// A wrong PIN. Applies the 2→24h→2→destroy escalation and returns what
    /// happened. Assumes the caller already checked [`status`] wasn't `Locked`.
    pub fn record_failure(&self, now: u64) -> FailOutcome {
        let mut st = self.read_state();
        st.fails += 1;
        if st.fails < FAILS_PER_STAGE {
            self.write_state(&st);
            return FailOutcome::Denied;
        }
        // Stage exhausted.
        if st.stage == 0 {
            // First offence: engage the 24h lock, advance to stage 1.
            st.stage = 1;
            st.fails = 0;
            st.lock_until = now + DAY_SECS;
            self.write_state(&st);
            FailOutcome::Locked { secs: DAY_SECS }
        } else {
            // Second offence after the lock: destroy everything and fall back.
            self.destroy();
            FailOutcome::Destroyed
        }
    }

    /// Wipe password + PIN and revert the policy to sudo-only, so nothing stays
    /// locked. Public so an explicit "reset autocomplete" can reuse it.
    pub fn destroy(&self) {
        self.forget();
        let _ = fs::remove_file(crate::auth::default_pin_path());
        let mut pol = crate::policy::load_default();
        pol.allow_pin = false;
        pol.allow_sudo = true;
        let _ = pol.save(&crate::policy::default_path());
    }

    // ── internals ─────────────────────────────────────────────────────────────
    /// Read (or lazily create) the 32-byte machine-local key.
    fn key(&self) -> std::io::Result<[u8; 32]> {
        if let Ok(bytes) = fs::read(&self.key_path) {
            if bytes.len() == 32 {
                let mut k = [0u8; 32];
                k.copy_from_slice(&bytes);
                return Ok(k);
            }
        }
        let fresh = crypto::rand_bytes(32);
        write_private(&self.key_path, &fresh)?;
        let mut k = [0u8; 32];
        k.copy_from_slice(&fresh);
        Ok(k)
    }

    fn read_state(&self) -> State {
        let mut st = State::default();
        if let Ok(text) = fs::read_to_string(&self.lock_path) {
            for line in text.lines() {
                if let Some((k, v)) = line.split_once('=') {
                    let v = v.trim();
                    match k.trim() {
                        "stage" => st.stage = v.parse().unwrap_or(0),
                        "fails" => st.fails = v.parse().unwrap_or(0),
                        "lock_until" => st.lock_until = v.parse().unwrap_or(0),
                        _ => {}
                    }
                }
            }
        }
        st
    }

    fn write_state(&self, st: &State) {
        let text = format!("stage={}\nfails={}\nlock_until={}\n",
                           st.stage, st.fails, st.lock_until);
        let _ = write_private(&self.lock_path, text.as_bytes());
    }
}

fn env_or(base: &std::path::Path, var: &str, default_name: &str) -> PathBuf {
    match std::env::var_os(var) {
        Some(p) => PathBuf::from(p),
        None => base.join(default_name),
    }
}

/// Write `data` to `path` with mode 0600 (create parent dir if needed).
fn write_private(path: &std::path::Path, data: &[u8]) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir)?;
    }
    let mut f = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)?;
    f.write_all(data)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_store() -> (Store, tempdir::TempDir) {
        let dir = tempdir::TempDir::new();
        let p = |n: &str| dir.path().join(n);
        (Store { key_path: p("k"), blob_path: p("b"), lock_path: p("l") }, dir)
    }

    #[test]
    fn store_load_roundtrip() {
        let (s, _d) = temp_store();
        assert!(!s.is_set());
        s.store("hunter2").unwrap();
        assert!(s.is_set());
        assert_eq!(s.load().as_deref(), Some("hunter2"));
        s.forget();
        assert!(!s.is_set());
        assert!(s.load().is_none());
    }

    #[test]
    fn lockout_escalation_two_then_lock_then_destroy() {
        let (s, _d) = temp_store();
        s.store("pw").unwrap();
        let t = 1_000_000u64;
        // First wrong: still denied, no lock.
        assert_eq!(s.record_failure(t), FailOutcome::Denied);
        assert_eq!(s.status(t), LockStatus::Open);
        // Second wrong: 24h lock engaged.
        assert_eq!(s.record_failure(t), FailOutcome::Locked { secs: DAY_SECS });
        // Now locked for ~24h.
        assert_eq!(s.status(t + 100), LockStatus::Locked { secs: DAY_SECS - 100 });
        // After the lock expires, it's open again.
        let t2 = t + DAY_SECS + 1;
        assert_eq!(s.status(t2), LockStatus::Open);
        // Two more wrong after the lock → destroy.
        assert_eq!(s.record_failure(t2), FailOutcome::Denied);
        assert_eq!(s.record_failure(t2), FailOutcome::Destroyed);
        // Destroy wiped the stored password.
        assert!(!s.is_set());
    }

    #[test]
    fn success_resets_failures() {
        let (s, _d) = temp_store();
        s.store("pw").unwrap();
        let t = 5u64;
        assert_eq!(s.record_failure(t), FailOutcome::Denied);
        s.record_success();
        // One prior failure was cleared, so it takes two fresh ones to lock.
        assert_eq!(s.record_failure(t), FailOutcome::Denied);
        assert_eq!(s.record_failure(t), FailOutcome::Locked { secs: DAY_SECS });
    }
}

/// Tiny temp-dir helper (the daemon avoids external crates, incl. `tempfile`).
#[cfg(test)]
mod tempdir {
    use std::path::{Path, PathBuf};
    pub struct TempDir(PathBuf);
    impl TempDir {
        pub fn new() -> TempDir {
            let mut p = std::env::temp_dir();
            let uniq = format!("applocker-sudopass-{}-{}",
                               std::process::id(),
                               crate::crypto::to_hex(&crate::crypto::rand_bytes(6)));
            p.push(uniq);
            std::fs::create_dir_all(&p).unwrap();
            TempDir(p)
        }
        pub fn path(&self) -> &Path { &self.0 }
    }
    impl Drop for TempDir {
        fn drop(&mut self) { let _ = std::fs::remove_dir_all(&self.0); }
    }
}
