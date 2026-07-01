//! Auth **policy** — the user-facing switches for how unlocking works.
//!
//! Distinct from [`crate::auth::Config`] (which is internal tunables like
//! attempt counts): this is the "what do I trust" choice the machine owner makes
//! and wants to persist —
//!
//!   - `face`     — try face recognition at all? (convenience vs. security)
//!   - `fallback` — which fallbacks are offered: PIN, sudo password, or both.
//!
//! Stored as a tiny `key = value` file (no TOML/JSON crate — we have no
//! crates.io access; see ../README of the daemon), default `/etc/applocker/config`:
//!
//! ```text
//! # AppLocker auth policy
//! face = off
//! fallback = pin, sudo
//! ```
//!
//! **Lock-out invariant:** at least one fallback is *always* enabled, even with
//! face on — a broken camera must never lock you out. Saving a policy with no
//! fallback is rejected here, and [`crate::auth::run`] also denies loudly at
//! runtime if it somehow ends up with none.

use std::fs;
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Policy {
    /// Try face recognition before falling back. Off = PIN/sudo only.
    pub face_enabled: bool,
    /// Offer the PIN fallback.
    pub allow_pin: bool,
    /// Offer the sudo-password fallback.
    pub allow_sudo: bool,
    /// Re-auth on *every* launch of a locked app, instead of caching the unlock
    /// until the next session lock. The global default; a future per-app list
    /// can override it. Maps to [`gate::CachePolicy`](crate::gate::CachePolicy).
    pub reauth_every_time: bool,
}

impl Default for Policy {
    fn default() -> Self {
        // Secure-and-convenient default: face off (opt in deliberately), both
        // fallbacks available so you can always get in, and unlock-once-per-
        // session (re-ask only after a lock) so it isn't nagging.
        Policy {
            face_enabled: false,
            allow_pin: true,
            allow_sudo: true,
            reauth_every_time: false,
        }
    }
}

impl Policy {
    /// Parse the file at `path`; a missing file yields the defaults. A malformed
    /// line is skipped with a warning rather than bricking auth.
    pub fn load(path: &Path) -> Policy {
        let mut p = Policy::default();
        let text = match fs::read_to_string(path) {
            Ok(t) => t,
            Err(_) => return p, // missing/unreadable → defaults
        };
        for (lineno, raw) in text.lines().enumerate() {
            let line = raw.split('#').next().unwrap_or("").trim();
            if line.is_empty() {
                continue;
            }
            let Some((key, val)) = line.split_once('=') else {
                eprintln!("applockerd: config:{}: ignoring malformed line", lineno + 1);
                continue;
            };
            let key = key.trim();
            let val = val.trim();
            match key {
                "face" => p.face_enabled = parse_bool(val).unwrap_or(p.face_enabled),
                "fallback" => {
                    let (pin, sudo) = parse_fallback(val);
                    p.allow_pin = pin;
                    p.allow_sudo = sudo;
                }
                // reauth = session (cache until lock) | always (every launch)
                "reauth" => p.reauth_every_time = parse_reauth(val).unwrap_or(p.reauth_every_time),
                other => eprintln!("applockerd: config:{}: unknown key {other:?}", lineno + 1),
            }
        }
        // Never let a parsed file violate the invariant.
        if !p.allow_pin && !p.allow_sudo {
            eprintln!("applockerd: config has no fallback enabled — forcing both on");
            p.allow_pin = true;
            p.allow_sudo = true;
        }
        p
    }

    /// Validate and write the policy `0644` (readable so the GUI can show it;
    /// nothing secret lives here). Rejects an empty fallback set.
    pub fn save(&self, path: &Path) -> std::io::Result<()> {
        if !self.allow_pin && !self.allow_sudo {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "at least one fallback (pin or sudo) must stay enabled",
            ));
        }
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut fallback = Vec::new();
        if self.allow_pin {
            fallback.push("pin");
        }
        if self.allow_sudo {
            fallback.push("sudo");
        }
        let body = format!(
            "# AppLocker auth policy — edit with `applockerd set-face` / `set-fallback`\n\
             face = {}\n\
             fallback = {}\n\
             reauth = {}\n",
            if self.face_enabled { "on" } else { "off" },
            fallback.join(", "),
            if self.reauth_every_time { "always" } else { "session" }
        );
        let mut f = fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o644)
            .open(path)?;
        f.write_all(body.as_bytes())?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o644))?;
        Ok(())
    }

    /// One-line human summary for `applockerd config` / test headers.
    pub fn summary(&self) -> String {
        let mut fb = Vec::new();
        if self.allow_pin {
            fb.push("PIN");
        }
        if self.allow_sudo {
            fb.push("sudo");
        }
        format!(
            "face {}, fallback: {}, re-auth: {}",
            if self.face_enabled { "ON" } else { "off" },
            fb.join(" + "),
            if self.reauth_every_time { "every launch" } else { "once per session" }
        )
    }
}

/// `always`/`every` → re-auth every launch (true); `session`/`once` → cache
/// until lock (false).
fn parse_reauth(v: &str) -> Option<bool> {
    match v.to_ascii_lowercase().as_str() {
        "always" | "every" | "everytime" | "every-time" => Some(true),
        "session" | "once" | "once-per-session" => Some(false),
        _ => None,
    }
}

fn parse_bool(v: &str) -> Option<bool> {
    match v.to_ascii_lowercase().as_str() {
        "on" | "true" | "yes" | "1" | "enabled" => Some(true),
        "off" | "false" | "no" | "0" | "disabled" => Some(false),
        _ => None,
    }
}

/// Parse a `fallback = ...` value into `(allow_pin, allow_sudo)`. Accepts a
/// comma list of `pin`/`sudo`, or the words `both`/`all`. An empty/none result
/// keeps the invariant by falling back to both-on (with a warning at the caller).
fn parse_fallback(v: &str) -> (bool, bool) {
    let low = v.to_ascii_lowercase();
    if low.trim() == "both" || low.trim() == "all" {
        return (true, true);
    }
    let mut pin = false;
    let mut sudo = false;
    for tok in low.split([',', ' ', '+']) {
        match tok.trim() {
            "pin" => pin = true,
            "sudo" | "password" => sudo = true,
            "" => {}
            other => eprintln!("applockerd: config: unknown fallback {other:?}"),
        }
    }
    if !pin && !sudo {
        (true, true)
    } else {
        (pin, sudo)
    }
}

/// `$APPLOCKER_CONFIG`, else `/etc/applocker/config`.
pub fn default_path() -> PathBuf {
    match std::env::var_os("APPLOCKER_CONFIG") {
        Some(p) => PathBuf::from(p),
        None => PathBuf::from("/etc/applocker/config"),
    }
}

/// Load the policy from the default path.
pub fn load_default() -> Policy {
    Policy::load(&default_path())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("applocker_policy_test_{}_{}", std::process::id(), name));
        p
    }

    #[test]
    fn defaults_when_missing() {
        let p = Policy::load(Path::new("/no/such/applocker/config"));
        assert_eq!(p, Policy::default());
        assert!(!p.face_enabled && p.allow_pin && p.allow_sudo);
    }

    #[test]
    fn roundtrip_face_off_sudo_only() {
        let path = tmp("sudo_only");
        let pol = Policy {
            face_enabled: false,
            allow_pin: false,
            allow_sudo: true,
            reauth_every_time: false,
        };
        pol.save(&path).unwrap();
        let loaded = Policy::load(&path);
        assert_eq!(loaded, pol);
        assert_eq!(
            loaded.summary(),
            "face off, fallback: sudo, re-auth: once per session"
        );
        let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o644);
        fs::remove_file(&path).unwrap();
    }

    #[test]
    fn roundtrip_face_on_both_every_launch() {
        let path = tmp("face_both");
        let pol = Policy {
            face_enabled: true,
            allow_pin: true,
            allow_sudo: true,
            reauth_every_time: true,
        };
        pol.save(&path).unwrap();
        let loaded = Policy::load(&path);
        assert_eq!(loaded, pol);
        assert!(loaded.summary().ends_with("re-auth: every launch"));
        fs::remove_file(&path).unwrap();
    }

    #[test]
    fn empty_fallback_rejected_on_save() {
        let pol = Policy {
            face_enabled: true,
            allow_pin: false,
            allow_sudo: false,
            reauth_every_time: false,
        };
        assert!(pol.save(&tmp("empty")).is_err());
    }

    #[test]
    fn empty_fallback_in_file_forced_to_both() {
        let path = tmp("bad_file");
        fs::write(&path, "face = on\nfallback = \n").unwrap();
        let p = Policy::load(&path);
        assert!(p.allow_pin && p.allow_sudo, "invariant: must have a fallback");
        fs::remove_file(&path).unwrap();
    }

    #[test]
    fn parsing_tolerates_comments_and_synonyms() {
        let path = tmp("synonyms");
        fs::write(
            &path,
            "# comment\nface = true   # inline\nfallback = pin\n",
        )
        .unwrap();
        let p = Policy::load(&path);
        assert!(p.face_enabled && p.allow_pin && !p.allow_sudo);
        fs::remove_file(&path).unwrap();
    }

    #[test]
    fn fallback_both_keyword() {
        assert_eq!(parse_fallback("both"), (true, true));
        assert_eq!(parse_fallback("pin, sudo"), (true, true));
        assert_eq!(parse_fallback("password"), (false, true));
        assert_eq!(parse_fallback("nonsense"), (true, true)); // invariant fallback
    }
}
