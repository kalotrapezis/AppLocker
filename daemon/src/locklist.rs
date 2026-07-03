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
        let before = self.apps.len();
        self.apps
            .retain(|a| a.key != query && a.name.to_lowercase() != q);
        before - self.apps.len()
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
