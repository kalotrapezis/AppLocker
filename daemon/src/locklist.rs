//! The persistent list of locked apps — what replaces step 1's single hard-coded
//! substring target. The gate consults it on every exec to decide whether a
//! launch needs auth.
//!
//! Stored at `/etc/applocker/locked-apps` (override `$APPLOCKER_LOCKED_APPS`),
//! one entry per line, tab-separated and self-describing:
//!
//! ```text
//! # kind    key                         name
//! native    /usr/bin/steam              Steam
//! flatpak   com.valvesoftware.Steam     Steam
//! ```
//!
//! Matching: for `native` entries we compare the **basename** of the exec'd
//! binary to the basename of the stored key, so a lock on `steam` catches it
//! whether it runs from `/usr/bin` or `/usr/local/bin`. `flatpak` entries match
//! by the app's install path — a flatpak's real binaries live under
//! `…/flatpak/app/<app-id>/…`, so the app-id (the stored key) is a path
//! component we can match on (see `LockList::matches`). `snap` isn't matched
//! yet (stored but not enforced — the gate notes this).

use std::fs;
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use crate::desktop::{base, AppKind, DesktopApp};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LockedApp {
    pub kind: AppKind,
    pub key: String,
    pub name: String,
}

impl LockedApp {
    pub fn from_desktop(app: &DesktopApp) -> LockedApp {
        LockedApp {
            kind: app.kind,
            key: app.key.clone(),
            name: app.name.clone(),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LockList {
    pub apps: Vec<LockedApp>,
}

impl LockList {
    /// Load from `path`; a missing file is an empty list. Malformed lines are
    /// skipped rather than failing — a bad line must never disable the gate.
    pub fn load(path: &Path) -> LockList {
        let text = match fs::read_to_string(path) {
            Ok(t) => t,
            Err(_) => return LockList::default(),
        };
        let mut apps = Vec::new();
        for raw in text.lines() {
            let line = raw.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let parts: Vec<&str> = line.splitn(3, '\t').collect();
            if parts.len() < 2 {
                continue;
            }
            let Some(kind) = AppKind::from_tag(parts[0].trim()) else {
                continue;
            };
            let key = parts[1].trim().to_string();
            let name = parts.get(2).map(|s| s.trim().to_string()).unwrap_or_else(|| key.clone());
            apps.push(LockedApp { kind, key, name });
        }
        LockList { apps }
    }

    pub fn save(&self, path: &Path) -> std::io::Result<()> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut body = String::from("# AppLocker locked apps — kind<TAB>key<TAB>name\n");
        for a in &self.apps {
            body.push_str(&format!("{}\t{}\t{}\n", a.kind.as_str(), a.key, a.name));
        }
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

    /// Add an app, deduped by (kind, key). Returns false if already present.
    pub fn add(&mut self, app: LockedApp) -> bool {
        if self.apps.iter().any(|a| a.kind == app.kind && a.key == app.key) {
            return false;
        }
        self.apps.push(app);
        true
    }

    /// Remove every entry matching `query` by key (exact) or name
    /// (case-insensitive). Returns how many were removed.
    pub fn remove(&mut self, query: &str) -> usize {
        let q = query.to_lowercase();
        let qbase = base(query).to_lowercase();
        let before = self.apps.len();
        // Match the same identities `lock-app` might have stored: exact key,
        // case-folded key, display name, or the basename (so a native lock on
        // `steam` vs `/usr/bin/steam` still removes). Lenient on purpose — a
        // failed remove that leaves a lock in place is worse than an over-match.
        self.apps.retain(|a| {
            let k = a.key.to_lowercase();
            !(a.key == query
                || k == q
                || a.name.to_lowercase() == q
                || base(&a.key).to_lowercase() == qbase)
        });
        before - self.apps.len()
    }

    /// A locked **flatpak** whose app-id appears as a token in the launching
    /// process' cmdline. Flatpaks exec their real binary inside a bwrap mount
    /// namespace, so the host path never matches — but the `flatpak`/`bwrap`
    /// exec's pre-exec cmdline still carries `flatpak run … <app-id> …`. Exact
    /// token match on the app-id, so unrelated bwrap uses (triggers, the
    /// system-helper, our own prompt) never false-hit.
    pub fn matches_flatpak_cmdline(&self, cmdline_tokens: &[String]) -> Option<&LockedApp> {
        self.apps.iter().find(|a| {
            a.kind == AppKind::Flatpak && cmdline_tokens.iter().any(|t| t == &a.key)
        })
    }

    /// The locked app matching an exec'd binary path, if any.
    ///
    /// - `native` — basename equality (a lock on `steam` catches it from any
    ///   `bin` dir).
    /// - `flatpak` — the app's real binaries live under
    ///   `…/flatpak/app/<app-id>/…` (both the system store `/var/lib/flatpak`
    ///   and the user store `~/.local/share/flatpak`). The app-id is a path
    ///   component, so we match any exec beneath that app's install dir — this
    ///   catches the launch whether it came from the menu or `flatpak run`, and
    ///   the first exec (the app's `bin/<app-id>` wrapper) is enough to prompt.
    /// - `appimage` — the `.AppImage` file's absolute path. Launching an
    ///   AppImage execs the file itself (before its internal squashfs mount), so
    ///   an exact path match on that first exec catches the launch. Precise (no
    ///   false hits on a same-named file elsewhere), unlike native's basename.
    /// - `snap` — not handled yet.
    pub fn matches(&self, exec_path: &str) -> Option<&LockedApp> {
        let exe_base = base(exec_path);
        self.apps.iter().find(|a| match a.kind {
            AppKind::Native => base(&a.key) == exe_base,
            AppKind::Flatpak => exec_path.contains(&format!("/flatpak/app/{}/", a.key)),
            AppKind::AppImage => a.key == exec_path,
            AppKind::Snap => false,
        })
    }
}

/// `$APPLOCKER_LOCKED_APPS`, else `/etc/applocker/locked-apps`.
pub fn default_path() -> PathBuf {
    match std::env::var_os("APPLOCKER_LOCKED_APPS") {
        Some(p) => PathBuf::from(p),
        None => PathBuf::from("/etc/applocker/locked-apps"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn native(key: &str, name: &str) -> LockedApp {
        LockedApp { kind: AppKind::Native, key: key.into(), name: name.into() }
    }

    #[test]
    fn matches_by_basename() {
        let mut l = LockList::default();
        l.add(native("/usr/bin/steam", "Steam"));
        assert!(l.matches("/usr/bin/steam").is_some());
        assert!(l.matches("/usr/local/bin/steam").is_some()); // basename match
        assert!(l.matches("/usr/bin/firefox").is_none());
    }

    #[test]
    fn flatpak_matched_by_app_install_path() {
        let mut l = LockList::default();
        l.add(LockedApp { kind: AppKind::Flatpak, key: "com.github.tchx84.Flatseal".into(), name: "Flatseal".into() });
        // The launcher/sandbox helpers execing plain `flatpak` are NOT matched…
        assert!(l.matches("/usr/bin/flatpak").is_none());
        assert!(l.matches("/usr/bin/bwrap").is_none());
        // …but the app's real binary under the system store IS.
        assert!(l.matches(
            "/var/lib/flatpak/app/com.github.tchx84.Flatseal/current/active/files/bin/com.github.tchx84.Flatseal"
        ).is_some());
        // …and under a per-user install too.
        assert!(l.matches(
            "/home/teo/.local/share/flatpak/app/com.github.tchx84.Flatseal/x86_64/stable/abc/files/bin/foo"
        ).is_some());
        // A different app-id must not match.
        assert!(l.matches(
            "/var/lib/flatpak/app/org.other.App/current/active/files/bin/org.other.App"
        ).is_none());
    }

    #[test]
    fn flatpak_matched_by_cmdline_app_id() {
        // Real case: the app execs inside bwrap so its path is remapped; we match
        // the launcher's cmdline app-id token instead (see gate probe on metal).
        let mut l = LockList::default();
        l.add(LockedApp { kind: AppKind::Flatpak,
                          key: "org.localsend.localsend_app".into(), name: "LocalSend".into() });
        let toks = |s: &str| s.split_whitespace().map(String::from).collect::<Vec<_>>();
        // The locked app's launch cmdline → matched.
        assert!(l.matches_flatpak_cmdline(&toks(
            "/usr/bin/flatpak run --branch=stable --arch=x86_64 --command=localsend \
             --file-forwarding org.localsend.localsend_app @@u @@")).is_some());
        // Unrelated bwrap uses (triggers, system-helper, our own prompt) → NOT matched.
        assert!(l.matches_flatpak_cmdline(&toks(
            "bwrap --unshare-ipc --ro-bind / / -- /usr/share/flatpak/triggers/mime-database.trigger")).is_none());
        assert!(l.matches_flatpak_cmdline(&toks(
            "python3 /usr/lib/applocker/auth_prompt.py --app AppLocker --methods pin,sudo")).is_none());
        // A different flatpak's launch → NOT matched.
        assert!(l.matches_flatpak_cmdline(&toks(
            "/usr/bin/flatpak run org.other.App")).is_none());
        // A native lock with the same string is not treated as a flatpak match.
        let mut n = LockList::default();
        n.add(native("org.localsend.localsend_app", "x"));
        assert!(n.matches_flatpak_cmdline(&toks("flatpak run org.localsend.localsend_app")).is_none());
    }

    #[test]
    fn appimage_matched_by_exact_path() {
        let mut l = LockList::default();
        l.add(LockedApp {
            kind: AppKind::AppImage,
            key: "/home/teo/AppImages/viber.appimage".into(),
            name: "Viber".into(),
        });
        // The exact file the AppImage launch execs is matched…
        assert!(l.matches("/home/teo/AppImages/viber.appimage").is_some());
        // …but a same-named file elsewhere is NOT (path-precise, unlike native).
        assert!(l.matches("/tmp/viber.appimage").is_none());
        // …and the internal squashfs binaries (post-mount) aren't double-gated.
        assert!(l.matches("/tmp/.mount_viberXY/AppRun").is_none());
    }

    #[test]
    fn snap_stored_but_not_matched() {
        let mut l = LockList::default();
        l.add(LockedApp { kind: AppKind::Snap, key: "spotify".into(), name: "Spotify".into() });
        assert_eq!(l.apps.len(), 1);
        assert!(l.matches("/snap/spotify/current/usr/bin/spotify").is_none());
    }

    #[test]
    fn add_dedupes() {
        let mut l = LockList::default();
        assert!(l.add(native("/usr/bin/steam", "Steam")));
        assert!(!l.add(native("/usr/bin/steam", "Steam again")));
        assert_eq!(l.apps.len(), 1);
    }

    #[test]
    fn remove_by_key_or_name() {
        let mut l = LockList::default();
        l.add(native("/usr/bin/steam", "Steam"));
        l.add(native("/usr/bin/zen", "Zen Browser"));
        assert_eq!(l.remove("zen browser"), 1); // case-insensitive name
        assert_eq!(l.remove("/usr/bin/steam"), 1); // exact key
        assert!(l.apps.is_empty());
    }

    #[test]
    fn save_load_roundtrip() {
        let mut path = std::env::temp_dir();
        path.push(format!("applocker_locklist_test_{}", std::process::id()));
        let mut l = LockList::default();
        l.add(native("/usr/bin/steam", "Steam"));
        l.add(LockedApp { kind: AppKind::Flatpak, key: "org.zen.Zen".into(), name: "Zen".into() });
        l.save(&path).unwrap();
        assert_eq!(LockList::load(&path), l);
        let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o644);
        fs::remove_file(&path).unwrap();
    }

    #[test]
    fn malformed_lines_skipped() {
        let mut path = std::env::temp_dir();
        path.push(format!("applocker_locklist_bad_{}", std::process::id()));
        fs::write(&path, "garbage line\nnative\t/usr/bin/x\tX\n\n# c\nbogus\tkey\n").unwrap();
        let l = LockList::load(&path);
        // Only the well-formed native line survives ("bogus" is not a valid kind).
        assert_eq!(l.apps.len(), 1);
        assert!(l.matches("/usr/bin/x").is_some());
        fs::remove_file(&path).unwrap();
    }
}
