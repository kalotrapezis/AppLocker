//! Installed-app enumeration from XDG `.desktop` files — the data behind the
//! settings "+" picker, and the way `lock-app` resolves a chosen app to
//! something the exec-gate can actually match.
//!
//! The gate sees the *resolved binary path* of an exec (via `/proc/self/fd`), so
//! a lock has to know that path. For a normal app that's straightforward
//! (`Exec=/usr/bin/steam %U` → `steam`). Two cases genuinely don't map to a
//! stable binary and are labelled as such:
//!   - **Flatpak** — `Exec=/usr/bin/flatpak run … com.foo.Bar`; every flatpak
//!     execs `flatpak`/`bwrap`, so the binary path can't tell them apart. We
//!     capture the app-id and mark the kind Flatpak (gating it needs cgroup/env
//!     inspection — a later step).
//!   - **Snap** — similar, via `snap run <name>`.
//!
//! Std-only INI-ish parsing; no `.desktop`/glib crate (no crates.io here).

use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AppKind {
    Native,
    Flatpak,
    Snap,
}

impl AppKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            AppKind::Native => "native",
            AppKind::Flatpak => "flatpak",
            AppKind::Snap => "snap",
        }
    }

    pub fn from_tag(s: &str) -> Option<AppKind> {
        match s {
            "native" => Some(AppKind::Native),
            "flatpak" => Some(AppKind::Flatpak),
            "snap" => Some(AppKind::Snap),
            _ => None,
        }
    }

    /// Can the exec-gate match this kind from a binary path today? Native by
    /// basename; Flatpak by its app-install path (the app-id is a path
    /// component — see `LockList::matches`). Snap isn't handled yet.
    pub fn is_gateable(&self) -> bool {
        matches!(self, AppKind::Native | AppKind::Flatpak)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DesktopApp {
    /// Desktop file id (basename without `.desktop`), e.g. `org.gnome.Calculator`.
    pub id: String,
    pub name: String,
    pub icon: Option<String>,
    pub kind: AppKind,
    /// The gate/lock key: the binary path for Native, the app-id for
    /// Flatpak/Snap.
    pub key: String,
}

/// The standard XDG application directories, most-specific first (user overrides
/// system), including the flatpak/snap export dirs.
fn app_dirs() -> Vec<PathBuf> {
    let mut dirs = Vec::new();
    if let Some(home) = std::env::var_os("HOME") {
        let home = PathBuf::from(home);
        dirs.push(home.join(".local/share/applications"));
        dirs.push(home.join(".local/share/flatpak/exports/share/applications"));
    }
    dirs.push(PathBuf::from("/usr/local/share/applications"));
    dirs.push(PathBuf::from("/usr/share/applications"));
    dirs.push(PathBuf::from("/var/lib/flatpak/exports/share/applications"));
    dirs.push(PathBuf::from("/var/lib/snapd/desktop/applications"));
    dirs
}

/// Enumerate visible installed applications, deduped by desktop id (first dir to
/// define an id wins, so user entries override system ones).
pub fn installed() -> Vec<DesktopApp> {
    let mut seen = std::collections::HashSet::new();
    let mut apps = Vec::new();
    for dir in app_dirs() {
        let entries = match fs::read_dir(&dir) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) != Some("desktop") {
                continue;
            }
            let id = match path.file_stem().and_then(|s| s.to_str()) {
                Some(s) => s.to_string(),
                None => continue,
            };
            if seen.contains(&id) {
                continue;
            }
            if let Some(app) = parse_desktop_file(&path, &id) {
                seen.insert(id);
                apps.push(app);
            }
        }
    }
    apps.sort_by_key(|a| a.name.to_lowercase());
    apps
}

fn parse_desktop_file(path: &Path, id: &str) -> Option<DesktopApp> {
    let text = fs::read_to_string(path).ok()?;
    let mut in_entry = false;
    let (mut name, mut exec, mut icon) = (None, None, None);
    let (mut no_display, mut hidden, mut is_app) = (false, false, true);

    for raw in text.lines() {
        let line = raw.trim();
        if line.starts_with('[') {
            in_entry = line == "[Desktop Entry]";
            continue;
        }
        if !in_entry {
            continue;
        }
        let Some((k, v)) = line.split_once('=') else { continue };
        match k.trim() {
            "Name" if name.is_none() => name = Some(v.trim().to_string()),
            "Exec" if exec.is_none() => exec = Some(v.trim().to_string()),
            "Icon" if icon.is_none() => icon = Some(v.trim().to_string()),
            "NoDisplay" => no_display = v.trim().eq_ignore_ascii_case("true"),
            "Hidden" => hidden = v.trim().eq_ignore_ascii_case("true"),
            "Type" => is_app = v.trim() == "Application",
            _ => {}
        }
    }

    if no_display || hidden || !is_app {
        return None;
    }
    let exec = exec?;
    let (kind, key) = resolve_exec(&exec)?;
    Some(DesktopApp {
        id: id.to_string(),
        name: name.unwrap_or_else(|| id.to_string()),
        icon,
        kind,
        key,
    })
}

/// Shells and interpreters that must **never** be used as a Native lock key —
/// gating `/bin/sh` or `/usr/bin/python3` would prompt on huge swathes of the
/// system. `sh -c` wrappers are unwrapped first (below); anything that still
/// resolves to one of these is treated as un-gateable (returns None). Gating
/// interpreted apps is the file-gate's job (see the top-level README).
const INTERPRETERS: &[&str] = &[
    "sh", "bash", "dash", "zsh", "ksh", "fish", "csh", "tcsh", "env", "exec",
    "python", "python2", "python3", "perl", "ruby", "node", "nodejs", "wine",
];

/// Turn a `.desktop` `Exec=` value into `(kind, key)`. `key` is the binary path
/// (Native) or app-id (Flatpak/Snap). Returns None if nothing safe to gate is
/// found (empty, or resolves to a shell/interpreter).
pub fn resolve_exec(exec: &str) -> Option<(AppKind, String)> {
    resolve_tokens(&tokenize(exec), 0)
}

/// Quote-aware tokenizer for a `.desktop` `Exec=` string. Handles single and
/// double quotes (no escape processing — good enough for launcher lines) and
/// drops field codes (%U %f …) and flatpak's `@@` markers at the top level.
fn tokenize(s: &str) -> Vec<String> {
    let mut toks = Vec::new();
    let mut cur = String::new();
    let mut quote: Option<char> = None;
    let mut has = false;
    for c in s.chars() {
        match quote {
            Some(q) => {
                if c == q {
                    quote = None;
                } else {
                    cur.push(c);
                }
            }
            None => match c {
                '\'' | '"' => {
                    quote = Some(c);
                    has = true;
                }
                c if c.is_whitespace() => {
                    if has {
                        toks.push(std::mem::take(&mut cur));
                        has = false;
                    }
                }
                _ => {
                    cur.push(c);
                    has = true;
                }
            },
        }
    }
    if has {
        toks.push(cur);
    }
    toks.retain(|t| !t.starts_with('%') && t != "@@" && t != "@@u");
    toks
}

fn is_var_assignment(t: &str) -> bool {
    match t.split_once('=') {
        Some((name, _)) => {
            !name.is_empty()
                && !name.contains('/')
                && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
        }
        None => false,
    }
}

/// Resolve already-tokenized argv into `(kind, key)`, unwrapping one level of
/// `sh -c '<cmd>'`. `depth` guards the recursion.
fn resolve_tokens(toks: &[String], depth: u32) -> Option<(AppKind, String)> {
    if depth > 2 || toks.is_empty() {
        return None;
    }

    // Skip a leading `env`/`exec` and any VAR=VALUE assignments.
    let mut i = 0;
    while i < toks.len() {
        let b = base(&toks[i]);
        let is_wrapper_word = matches!(b, "env" | "exec") && !toks[i].contains('=');
        if is_wrapper_word || is_var_assignment(&toks[i]) {
            i += 1;
        } else {
            break;
        }
    }
    if i >= toks.len() {
        return None;
    }
    let argv0 = &toks[i];
    let rest = &toks[i + 1..];
    let b = base(argv0);

    // Unwrap `sh -c '<command>'` → resolve the inner command.
    if matches!(b, "sh" | "bash" | "dash" | "zsh" | "ksh")
        && rest.first().map(|s| s.as_str()) == Some("-c")
    {
        if let Some(inner) = rest.get(1) {
            return resolve_tokens(&tokenize(inner), depth + 1);
        }
    }

    if b == "flatpak" && rest.iter().any(|t| t == "run") {
        let run_at = rest.iter().position(|t| t == "run").unwrap();
        let appid = rest[run_at + 1..].iter().rev().find(|t| !t.starts_with('-'))?;
        return Some((AppKind::Flatpak, appid.clone()));
    }
    if b == "snap" && rest.first().map(|s| s.as_str()) == Some("run") {
        return rest.get(1).map(|n| (AppKind::Snap, n.clone()));
    }

    // Refuse to gate a bare shell/interpreter — unsafe as an exec-gate key.
    if INTERPRETERS.contains(&b) {
        return None;
    }
    Some((AppKind::Native, argv0.clone()))
}

/// Basename of a path-ish token.
pub fn base(s: &str) -> &str {
    s.rsplit('/').next().unwrap_or(s)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_native_plain() {
        assert_eq!(
            resolve_exec("/usr/bin/gnome-calculator"),
            Some((AppKind::Native, "/usr/bin/gnome-calculator".to_string()))
        );
    }

    #[test]
    fn resolve_native_with_field_codes_and_env() {
        assert_eq!(
            resolve_exec("env BAMF_DESKTOP_FILE_HINT=/x/y.desktop /usr/bin/zen-browser %u"),
            Some((AppKind::Native, "/usr/bin/zen-browser".to_string()))
        );
        assert_eq!(
            resolve_exec("steam %U"),
            Some((AppKind::Native, "steam".to_string()))
        );
    }

    #[test]
    fn resolve_unwraps_sh_c_wrapper() {
        // Steam's real launcher — must resolve to `steam`, never `sh`.
        assert_eq!(
            resolve_exec("sh -c 'STEAM_FRAME_FORCE_CLOSE=1 steam %U'"),
            Some((AppKind::Native, "steam".to_string()))
        );
        assert_eq!(
            resolve_exec("bash -c \"exec /usr/bin/foo --flag\""),
            Some((AppKind::Native, "/usr/bin/foo".to_string()))
        );
    }

    #[test]
    fn refuses_bare_shell_or_interpreter() {
        // A bare shell/interpreter must never become a lock key.
        assert_eq!(resolve_exec("/bin/sh"), None);
        assert_eq!(resolve_exec("bash"), None);
        assert_eq!(resolve_exec("python3 /usr/share/foo/app.py"), None);
        // ...even hidden behind a wrapper.
        assert_eq!(resolve_exec("sh -c 'python3 thing.py'"), None);
    }

    #[test]
    fn resolve_flatpak() {
        let e = "/usr/bin/flatpak run --branch=stable --arch=x86_64 \
                 com.valvesoftware.Steam @@u %U @@";
        assert_eq!(
            resolve_exec(e),
            Some((AppKind::Flatpak, "com.valvesoftware.Steam".to_string()))
        );
    }

    #[test]
    fn resolve_snap() {
        assert_eq!(
            resolve_exec("snap run spotify %U"),
            Some((AppKind::Snap, "spotify".to_string()))
        );
    }

    #[test]
    fn kind_gateability() {
        assert!(AppKind::Native.is_gateable());
        assert!(AppKind::Flatpak.is_gateable()); // now matched by app-install path
        assert!(!AppKind::Snap.is_gateable());
        assert_eq!(AppKind::from_tag(AppKind::Snap.as_str()), Some(AppKind::Snap));
    }

    #[test]
    fn base_of() {
        assert_eq!(base("/usr/bin/steam"), "steam");
        assert_eq!(base("steam"), "steam");
    }
}
