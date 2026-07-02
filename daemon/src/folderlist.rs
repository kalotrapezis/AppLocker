//! The persistent list of locked folders — the "files and folders" half of the
//! fence. The file-gate consults it on every file open: opening anything under a
//! locked folder requires auth (then it's cached until the next lock, like apps).
//!
//! Stored at `/etc/applocker/locked-folders` (override `$APPLOCKER_LOCKED_FOLDERS`),
//! one entry per line, tab-separated:
//!
//! ```text
//! # path                       name
//! /home/teo/Documents          Documents
//! ```
//!
//! Matching is by **canonical path prefix** (component-wise, so `/home/teo/Docs`
//! never matches `/home/teo/Docs2`). Paths are canonicalised at lock time and the
//! opened path from `/proc/self/fd` is already canonical, so the comparison is a
//! plain `starts_with`.
//!
//! Safety: [`is_safe_to_lock`] refuses system roots (`/`, `/etc`, `/usr`, …) — a
//! file-gate on those would prompt on essentially every process, including the
//! daemon's own reads, and could wedge the machine.

use std::fs;
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LockedFolder {
    /// Canonical absolute path of the locked directory.
    pub path: String,
    pub name: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FolderList {
    pub folders: Vec<LockedFolder>,
}

impl FolderList {
    pub fn load(path: &Path) -> FolderList {
        let text = match fs::read_to_string(path) {
            Ok(t) => t,
            Err(_) => return FolderList::default(),
        };
        let mut folders = Vec::new();
        for raw in text.lines() {
            let line = raw.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let (p, name) = match line.split_once('\t') {
                Some((p, n)) => (p.trim().to_string(), n.trim().to_string()),
                None => (line.to_string(), base_name(line)),
            };
            if p.is_empty() {
                continue;
            }
            folders.push(LockedFolder { path: p, name });
        }
        FolderList { folders }
    }

    pub fn save(&self, path: &Path) -> std::io::Result<()> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut body = String::from("# AppLocker locked folders — path<TAB>name\n");
        for f in &self.folders {
            body.push_str(&format!("{}\t{}\n", f.path, f.name));
        }
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o644)
            .open(path)?;
        file.write_all(body.as_bytes())?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o644))?;
        Ok(())
    }

    /// Add a folder, deduped by path. Returns false if already present.
    pub fn add(&mut self, folder: LockedFolder) -> bool {
        if self.folders.iter().any(|f| f.path == folder.path) {
            return false;
        }
        self.folders.push(folder);
        true
    }

    /// Remove entries matching `query` by exact path or (case-insensitive) name.
    pub fn remove(&mut self, query: &str) -> usize {
        let q = query.to_lowercase();
        let before = self.folders.len();
        self.folders
            .retain(|f| f.path != query && f.name.to_lowercase() != q);
        before - self.folders.len()
    }

    pub fn is_empty(&self) -> bool {
        self.folders.is_empty()
    }

    /// The locked folder containing `open_path`, if any. A locked folder matches
    /// itself and everything beneath it.
    pub fn matches(&self, open_path: &str) -> Option<&LockedFolder> {
        let opened = Path::new(open_path);
        self.folders.iter().find(|f| {
            let base = Path::new(&f.path);
            opened == base || opened.starts_with(base)
        })
    }
}

/// System roots we refuse to lock — gating opens under these would prompt on
/// nearly every process (the daemon included) and can wedge the machine.
const REFUSED: &[&str] = &[
    "/", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt", "/proc",
    "/sys", "/dev", "/run", "/boot", "/var",
];

/// Validate a candidate folder: must be an existing directory and not a system
/// root or an ancestor of AppLocker's own config. Returns the canonical path.
pub fn is_safe_to_lock(input: &str) -> Result<String, String> {
    let canon = fs::canonicalize(input)
        .map_err(|e| format!("{input}: {e}"))?;
    if !canon.is_dir() {
        return Err(format!("{} is not a directory", canon.display()));
    }
    let canon_str = canon.to_string_lossy().into_owned();
    if REFUSED.iter().any(|r| canon_str == *r) {
        return Err(format!("{canon_str} is a system directory and can't be locked"));
    }
    // Refuse anything that would contain AppLocker's own moving parts. Locking
    // such a folder gates the very tools needed to unlock it (recognize.py, the
    // prompt, enrolled faces) — a guaranteed self-gating wedge that can freeze
    // the whole desktop (learned the hard way: user locked ~/Documents, which
    // contained this repo, and had to power-cycle).
    for (what, p) in self_paths() {
        if p.starts_with(&canon) {
            return Err(format!(
                "{canon_str} contains {what} ({}) — locking it would gate AppLocker itself",
                p.display()
            ));
        }
    }
    Ok(canon_str)
}

/// Paths that must never end up inside a locked folder.
fn self_paths() -> Vec<(&'static str, PathBuf)> {
    let mut v: Vec<(&'static str, PathBuf)> = vec![
        ("AppLocker's config", PathBuf::from("/etc/applocker")),
    ];
    // The daemon binary — in the repo layout the GUI helper scripts live in the
    // same tree (<repo>/daemon/target/... vs <repo>/gui), so refusing the exe's
    // path covers them too; the installed layout is under /usr (already refused).
    if let Ok(exe) = std::env::current_exe() {
        v.push(("the AppLocker daemon", exe));
    }
    // The invoking user's enrolled faces / models.
    if let Ok(user) = std::env::var("SUDO_USER") {
        if !user.is_empty() {
            v.push(("your enrolled faces", PathBuf::from(format!("/home/{user}/.config/applocker"))));
        }
    } else if let Some(home) = std::env::var_os("HOME") {
        v.push(("your enrolled faces", PathBuf::from(home).join(".config/applocker")));
    }
    v
}

fn base_name(p: &str) -> String {
    Path::new(p)
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| p.to_string())
}

/// `$APPLOCKER_LOCKED_FOLDERS`, else `/etc/applocker/locked-folders`.
pub fn default_path() -> PathBuf {
    match std::env::var_os("APPLOCKER_LOCKED_FOLDERS") {
        Some(p) => PathBuf::from(p),
        None => PathBuf::from("/etc/applocker/locked-folders"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn folder(path: &str, name: &str) -> LockedFolder {
        LockedFolder { path: path.into(), name: name.into() }
    }

    #[test]
    fn prefix_matching_is_component_wise() {
        let mut l = FolderList::default();
        l.add(folder("/home/teo/Documents", "Documents"));
        assert!(l.matches("/home/teo/Documents").is_some()); // the dir itself
        assert!(l.matches("/home/teo/Documents/tax/2025.pdf").is_some()); // nested
        assert!(l.matches("/home/teo/Documents2/x").is_none()); // NOT a prefix
        assert!(l.matches("/home/teo/Pictures/x").is_none());
    }

    #[test]
    fn dedupe_and_remove() {
        let mut l = FolderList::default();
        assert!(l.add(folder("/home/teo/Documents", "Documents")));
        assert!(!l.add(folder("/home/teo/Documents", "Docs again")));
        assert_eq!(l.remove("documents"), 1); // by name, case-insensitive
        assert!(l.is_empty());
    }

    #[test]
    fn save_load_roundtrip() {
        let mut path = std::env::temp_dir();
        path.push(format!("applocker_folders_test_{}", std::process::id()));
        let mut l = FolderList::default();
        l.add(folder("/home/teo/Documents", "Documents"));
        l.add(folder("/home/teo/Secret Stuff", "Secret Stuff"));
        l.save(&path).unwrap();
        assert_eq!(FolderList::load(&path), l);
        let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o644);
        fs::remove_file(&path).unwrap();
    }

    #[test]
    fn refuses_system_roots() {
        assert!(is_safe_to_lock("/").is_err());
        assert!(is_safe_to_lock("/etc").is_err());
        assert!(is_safe_to_lock("/usr").is_err());
    }

    #[test]
    fn accepts_a_real_user_dir() {
        // Use the temp dir as a stand-in for a normal lockable directory.
        let tmp = std::env::temp_dir();
        let ok = is_safe_to_lock(tmp.to_str().unwrap());
        assert!(ok.is_ok(), "temp dir should be lockable: {ok:?}");
    }
}
