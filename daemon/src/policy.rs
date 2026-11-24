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

/// What a single tier accepts. The three tiers are deliberately separate: an
/// app launch, the lock screen and a file vault are different stakes, and the
/// user asked for them to be configured apart.
///
/// `require_both` means the two biometrics must agree. Liveness is dropped in
/// that mode on purpose — a second factor is stronger evidence than a head
/// turn, and asking for both at once is what makes people turn it off.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TierPolicy {
    pub face: bool,
    pub fingerprint: bool,
    pub require_both: bool,
    pub pin: bool,
    pub password: bool,
}

impl TierPolicy {
    /// Liveness is on unless the tier leans on two factors instead.
    pub fn liveness(&self) -> bool {
        !(self.require_both && self.face && self.fingerprint)
    }

    /// Does this tier have any biometric at all?
    pub fn any_biometric(&self) -> bool {
        self.face || self.fingerprint
    }

    /// Public wrapper so the CLI can parse a user-supplied list.
    pub fn parse_methods_public(val: &str, into: &mut TierPolicy) {
        Self::parse_methods(val, into)
    }

    fn parse_methods(val: &str, into: &mut TierPolicy) {
        let (mut face, mut fp, mut pin, mut pw, mut both) = (false, false, false, false, false);
        for tok in val.split(&[',', '+'][..]).map(|t| t.trim()) {
            match tok {
                "face" => face = true,
                "fingerprint" | "finger" | "fp" => fp = true,
                "pin" => pin = true,
                "password" | "sudo" => pw = true,
                "both" | "require-both" => both = true,
                "" | "none" => {}
                other => eprintln!("applockerd: unknown method {other:?} — ignored"),
            }
        }
        *into = TierPolicy { face, fingerprint: fp, require_both: both, pin, password: pw };
    }

    fn to_methods(self) -> String {
        let mut v = Vec::new();
        if self.fingerprint {
            v.push("fingerprint");
        }
        if self.face {
            v.push("face");
        }
        if self.require_both {
            v.push("both");
        }
        if self.pin {
            v.push("pin");
        }
        if self.password {
            v.push("password");
        }
        if v.is_empty() {
            "none".to_string()
        } else {
            v.join(", ")
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Policy {
    /// Try face recognition before falling back. Off = PIN/sudo only.
    pub face_enabled: bool,
    /// Offer the PIN fallback.
    pub allow_pin: bool,
    /// Offer the sudo-password fallback.
    pub allow_sudo: bool,
    /// Try the fingerprint reader before face. Off = never touch fprintd, so a
    /// reader you don't trust (or don't want waited on) is out of the picture.
    pub allow_fingerprint: bool,
    /// Re-auth on *every* launch of a locked app, instead of caching the unlock
    /// until the next session lock. The global default; a future per-app list
    /// can override it. Maps to [`gate::CachePolicy`](crate::gate::CachePolicy).
    pub reauth_every_time: bool,
    /// The presence watcher (dim when away, lock when gone). The watcher script
    /// (face/watch_presence.py, run in the user session) reads this key.
    pub attention_enabled: bool,
    /// Minutes between presence snapshots. One of [`ATTENTION_INTERVALS`]; the
    /// watcher polls this to space out how often it wakes the camera.
    pub attention_interval_min: u32,
    /// Only run the watcher while on AC power; on battery it disables itself
    /// (the camera is the biggest drain, and a laptop on battery is usually
    /// with you). The watcher reads this and pauses when unplugged.
    pub attention_ac_only: bool,
    /// Master switch for app-locking. When off, the gate ignores the locked-app
    /// list entirely (nothing prompts on launch) while the list itself is kept,
    /// so the feature can be toggled without losing which apps were chosen.
    pub apps_enabled: bool,
    /// Per-tier method sets. Absent from an older config file, in which case
    /// they are derived from the global switches above so nothing changes.
    pub apps: TierPolicy,
    pub lockscreen: TierPolicy,
    pub files: TierPolicy,
}

/// The intervals the UI offers (minutes between snapshots). A hand-edited value
/// outside this set is rejected back to the default.
pub const ATTENTION_INTERVALS: [u32; 5] = [2, 5, 10, 15, 30];
const ATTENTION_INTERVAL_DEFAULT: u32 = 2;

impl Default for Policy {
    fn default() -> Self {
        // Secure-and-convenient default: face off (opt in deliberately), both
        // fallbacks available so you can always get in, and unlock-once-per-
        // session (re-ask only after a lock) so it isn't nagging.
        Policy {
            face_enabled: false,
            // On when a finger is actually enrolled — the check is live, so
            // this costs nothing on a machine with no reader.
            allow_fingerprint: true,
            allow_pin: true,
            allow_sudo: true,
            reauth_every_time: false,
            attention_enabled: false,
            attention_interval_min: ATTENTION_INTERVAL_DEFAULT,
            attention_ac_only: false,
            // App-locking on by default: a fresh install with no locked apps is
            // harmless, and this keeps existing configs (which never wrote the
            // key) behaving exactly as before the master switch existed.
            apps_enabled: true,
            // Derived in `load` from the global switches when the per-tier keys
            // are absent, which is every config written before this existed.
            apps: TierPolicy { face: false, fingerprint: true, require_both: false,
                               pin: true, password: true },
            lockscreen: TierPolicy { face: false, fingerprint: true, require_both: false,
                                     pin: false, password: true },
            files: TierPolicy { face: false, fingerprint: true, require_both: false,
                                pin: true, password: true },
        }
    }
}

impl Policy {
    /// Parse the file at `path`; a missing file yields the defaults. A malformed
    /// line is skipped with a warning rather than bricking auth.
    pub fn load(path: &Path) -> Policy {
        let mut p = Policy::default();
        // Which tiers the file actually configured; the rest inherit the global
        // switches, so an older config keeps behaving exactly as it did.
        let mut seen_tiers: std::collections::HashSet<&str> = std::collections::HashSet::new();
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
                "fingerprint" => {
                    p.allow_fingerprint = parse_bool(val).unwrap_or(p.allow_fingerprint)
                }
                "apps_methods" => {
                    TierPolicy::parse_methods(val, &mut p.apps);
                    seen_tiers.insert("apps");
                }
                "lockscreen_methods" => {
                    TierPolicy::parse_methods(val, &mut p.lockscreen);
                    seen_tiers.insert("lockscreen");
                }
                "files_methods" => {
                    TierPolicy::parse_methods(val, &mut p.files);
                    seen_tiers.insert("files");
                }
                "fallback" => {
                    let (pin, sudo) = parse_fallback(val);
                    p.allow_pin = pin;
                    p.allow_sudo = sudo;
                }
                // reauth = session (cache until lock) | always (every launch)
                "reauth" => p.reauth_every_time = parse_reauth(val).unwrap_or(p.reauth_every_time),
                "attention" => {
                    p.attention_enabled = parse_bool(val).unwrap_or(p.attention_enabled)
                }
                "attention_interval" => {
                    p.attention_interval_min =
                        parse_interval(val).unwrap_or(p.attention_interval_min)
                }
                "attention_ac_only" => {
                    p.attention_ac_only = parse_bool(val).unwrap_or(p.attention_ac_only)
                }
                "apps_enabled" => {
                    p.apps_enabled = parse_bool(val).unwrap_or(p.apps_enabled)
                }
                other => eprintln!("applockerd: config:{}: unknown key {other:?}", lineno + 1),
            }
        }
        // Never let a parsed file violate the invariant.
        if !p.allow_pin && !p.allow_sudo {
            eprintln!("applockerd: config has no fallback enabled — forcing both on");
            p.allow_pin = true;
            p.allow_sudo = true;
        }
        // The global switches are masters: a tier may narrow what is offered,
        // never widen it past them. Without this, "Face unlock: off" in Settings
        // left apps and files still asking for a face, because their method
        // lists were written while it was on.
        let clamp = |t: &mut TierPolicy, face_on: bool, fp_on: bool| {
            t.face &= face_on;
            t.fingerprint &= fp_on;
            t.require_both &= t.face && t.fingerprint;
        };
        let inherited = TierPolicy {
            face: p.face_enabled,
            fingerprint: p.allow_fingerprint,
            require_both: false,
            pin: p.allow_pin,
            password: p.allow_sudo,
        };
        if !seen_tiers.contains("apps") {
            p.apps = inherited;
        }
        if !seen_tiers.contains("lockscreen") {
            // No PIN at the lock screen: the greeter takes the account password.
            p.lockscreen = TierPolicy { pin: false, ..inherited };
        }
        if !seen_tiers.contains("files") {
            p.files = inherited;
        }
        let (face_on, fp_on) = (p.face_enabled, p.allow_fingerprint);
        clamp(&mut p.apps, face_on, fp_on);
        clamp(&mut p.lockscreen, face_on, fp_on);
        clamp(&mut p.files, face_on, fp_on);
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
             fingerprint = {}\n\
             fallback = {}\n\
             reauth = {}\n\
             attention = {}\n\
             attention_interval = {}\n\
             attention_ac_only = {}\n\
             apps_enabled = {}\n\
             apps_methods = {}\n\
             lockscreen_methods = {}\n\
             files_methods = {}\n",
            if self.face_enabled { "on" } else { "off" },
            if self.allow_fingerprint { "on" } else { "off" },
            fallback.join(", "),
            if self.reauth_every_time { "always" } else { "session" },
            if self.attention_enabled { "on" } else { "off" },
            self.attention_interval_min,
            if self.attention_ac_only { "on" } else { "off" },
            if self.apps_enabled { "on" } else { "off" },
            self.apps.to_methods(),
            self.lockscreen.to_methods(),
            self.files.to_methods()
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
        let att = if self.attention_enabled {
            format!(
                "on (every {}m{})",
                self.attention_interval_min,
                if self.attention_ac_only { ", AC only" } else { "" }
            )
        } else {
            "off".to_string()
        };
        format!(
            "face {}, fingerprint {}, fallback: {}, re-auth: {}, attention {}, apps {}",
            if self.face_enabled { "ON" } else { "off" },
            if self.allow_fingerprint { "on" } else { "off" },
            fb.join(" + "),
            if self.reauth_every_time { "every launch" } else { "once per session" },
            att,
            if self.apps_enabled { "on" } else { "off" }
        )
    }
}

/// Parse `attention_interval = <minutes>`; only the [`ATTENTION_INTERVALS`]
/// values are accepted (anything else → None, keeping the previous value).
fn parse_interval(v: &str) -> Option<u32> {
    let n: u32 = v.trim().parse().ok()?;
    ATTENTION_INTERVALS.contains(&n).then_some(n)
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

    /// Each tier is parsed, written and read back as the user wrote it, and
    /// "both" is what turns liveness off.
    #[test]
    fn tier_methods_roundtrip_and_liveness() {
        let mut t = TierPolicy { face: false, fingerprint: false, require_both: false,
                                 pin: false, password: false };
        TierPolicy::parse_methods("fingerprint, face, both", &mut t);
        assert!(t.face && t.fingerprint && t.require_both);
        assert!(!t.liveness(), "two factors replace the head turn");
        assert!(!t.pin && !t.password);
        assert_eq!(t.to_methods(), "fingerprint, face, both");

        TierPolicy::parse_methods("face", &mut t);
        assert!(t.liveness(), "one biometric keeps liveness");
        assert!(!t.require_both);

        TierPolicy::parse_methods("fp+pin", &mut t);
        assert!(t.fingerprint && t.pin && !t.face);

        TierPolicy::parse_methods("none", &mut t);
        assert!(!t.any_biometric() && !t.pin && !t.password);
    }

    /// The global switches are masters: turning face off in Settings must stop
    /// apps and files asking for it, even though their lists still name it.
    #[test]
    fn global_switches_clamp_the_tiers() {
        let path = tmp("clamp");
        std::fs::write(
            &path,
            "face = off\nfingerprint = on\nfallback = sudo\n\
             apps_methods = fingerprint, face, password\n\
             files_methods = fingerprint, face, both, password\n",
        )
        .unwrap();
        let p = Policy::load(&path);
        assert!(!p.apps.face, "face off globally → not used for apps");
        assert!(p.apps.fingerprint, "fingerprint stays, it is on globally");
        assert!(!p.files.face);
        assert!(!p.files.require_both, "cross-check needs both factors present");
        assert!(p.files.password, "non-biometric methods are untouched");
        let _ = std::fs::remove_file(&path);
    }

    /// An old config with no per-tier keys must behave exactly as before.
    #[test]
    fn tiers_inherit_the_global_switches() {
        let path = tmp("inherit");
        std::fs::write(&path, "face = on\nfingerprint = off\nfallback = pin\n").unwrap();
        let p = Policy::load(&path);
        assert!(p.apps.face && !p.apps.fingerprint && p.apps.pin && !p.apps.password);
        assert!(p.lockscreen.face, "lock screen inherits face too");
        assert!(!p.lockscreen.pin, "but never the PIN: the greeter takes a password");
        assert!(p.files.pin);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn roundtrip_face_off_sudo_only() {
        let path = tmp("sudo_only");
        let pol = Policy {
            face_enabled: false,
            allow_fingerprint: true,
            allow_pin: false,
            allow_sudo: true,
            reauth_every_time: false,
            attention_enabled: false,
            attention_interval_min: 2,
            attention_ac_only: false,
            apps_enabled: true,
            apps: TierPolicy { face: false, fingerprint: true, require_both: false,
                               pin: true, password: true },
            lockscreen: TierPolicy { face: false, fingerprint: true, require_both: false,
                                     pin: false, password: true },
            files: TierPolicy { face: false, fingerprint: true, require_both: false,
                                pin: true, password: true },
        };
        pol.save(&path).unwrap();
        let loaded = Policy::load(&path);
        assert_eq!(loaded, pol);
        assert_eq!(
            loaded.summary(),
            "face off, fingerprint on, fallback: sudo, re-auth: once per session, attention off, apps on"
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
            allow_fingerprint: true,
            allow_pin: true,
            allow_sudo: true,
            reauth_every_time: true,
            attention_enabled: false,
            attention_interval_min: 2,
            attention_ac_only: false,
            apps_enabled: true,
            apps: TierPolicy { face: false, fingerprint: true, require_both: false,
                               pin: true, password: true },
            lockscreen: TierPolicy { face: false, fingerprint: true, require_both: false,
                                     pin: false, password: true },
            files: TierPolicy { face: false, fingerprint: true, require_both: false,
                                pin: true, password: true },
        };
        pol.save(&path).unwrap();
        let loaded = Policy::load(&path);
        assert_eq!(loaded, pol);
        assert!(loaded.summary().contains("re-auth: every launch"));
        fs::remove_file(&path).unwrap();
    }

    #[test]
    fn empty_fallback_rejected_on_save() {
        let pol = Policy {
            face_enabled: true,
            allow_fingerprint: true,
            allow_pin: false,
            allow_sudo: false,
            reauth_every_time: false,
            attention_enabled: false,
            attention_interval_min: 2,
            attention_ac_only: false,
            apps_enabled: true,
            apps: TierPolicy { face: false, fingerprint: true, require_both: false,
                               pin: true, password: true },
            lockscreen: TierPolicy { face: false, fingerprint: true, require_both: false,
                                     pin: false, password: true },
            files: TierPolicy { face: false, fingerprint: true, require_both: false,
                                pin: true, password: true },
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
    fn attention_interval_and_ac_roundtrip() {
        let path = tmp("attn_interval");
        let mut pol = Policy::default();
        pol.attention_enabled = true;
        pol.attention_interval_min = 15;
        pol.attention_ac_only = true;
        pol.save(&path).unwrap();
        let loaded = Policy::load(&path);
        assert_eq!(loaded, pol);
        assert_eq!(loaded.summary().contains("on (every 15m, AC only)"), true);
        fs::remove_file(&path).unwrap();
    }

    #[test]
    fn attention_interval_rejects_bad_value() {
        // A value outside the allowed set keeps the default (2), not 7.
        let path = tmp("attn_bad");
        fs::write(&path, "attention = on\nattention_interval = 7\n").unwrap();
        let p = Policy::load(&path);
        assert_eq!(p.attention_interval_min, 2);
        for good in ATTENTION_INTERVALS {
            assert_eq!(parse_interval(&good.to_string()), Some(good));
        }
        assert_eq!(parse_interval("0"), None);
        assert_eq!(parse_interval("abc"), None);
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
