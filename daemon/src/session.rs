//! Running the daemon's GUI/camera children in the **active user's graphical
//! session**, even though the daemon itself is root.
//!
//! When `applockerd` runs as a system service it has no `DISPLAY`, so it can't
//! show the GTK unlock prompt or reach the webcam as the user. This module finds
//! the logged-in session and drops each spawned child into it:
//!
//!   1. ask logind (`loginctl`) for the active seat0 session — its user + X
//!      display;
//!   2. read that user's live `DISPLAY` / `XAUTHORITY` / `XDG_RUNTIME_DIR` from a
//!      running session process' `/proc/<pid>/environ` (readable because we're
//!      root — this is authoritative for the real Xauthority path);
//!   3. spawn the child with those env vars and drop privileges to the user
//!      (`initgroups` + `setgid` + `setuid`), so it's an ordinary session
//!      process — which also grants the `video` group needed for the camera.
//!
//! Everything degrades safely: not root, no logind, or nobody logged in →
//! [`attach`] is a no-op and the child inherits the daemon's environment (the
//! old `sudo applockerd` behaviour).

use std::ffi::CStr;
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::Command;

#[derive(Debug, Clone)]
pub struct SessionCtx {
    pub uid: u32,
    pub gid: u32,
    pub user: String,
    pub home: PathBuf,
    /// X11 / XWayland `$DISPLAY` (e.g. `:0`), if the session has one. Empty on a
    /// pure-Wayland session, which is why it's optional now.
    pub display: Option<String>,
    /// Wayland `$WAYLAND_DISPLAY` (e.g. `wayland-0`). This is how GTK reaches a
    /// KDE/Wayland compositor — the missing piece for the SDDM/Wayland port.
    pub wayland_display: Option<String>,
    pub xauthority: Option<String>,
    pub xdg_runtime_dir: String,
}

/// If we're root, arrange for `cmd` to run in the active user's graphical
/// session (env + dropped privileges). Returns the context used, so callers can
/// also resolve the user's home (models/enrolled faces live there). A no-op
/// returning `None` when not root, or when no graphical session is found.
pub fn attach(cmd: &mut Command) -> Option<SessionCtx> {
    if unsafe { libc::geteuid() } != 0 {
        return None; // can't setuid as non-root; inherit our env (dev/sudo run)
    }
    let ctx = SessionCtx::discover()?;
    if ctx.uid == 0 {
        return None; // root's own "session" — nothing to drop into
    }
    ctx.apply(cmd);
    Some(ctx)
}

impl SessionCtx {
    /// Find the active graphical session, or `None` (headless / not logged in).
    pub fn discover() -> Option<SessionCtx> {
        let (uid, display0) = active_session()?;
        let (user, gid, home) = passwd(uid)?;
        let env = scan_environ(uid);
        let display = env.as_ref().and_then(|e| e.display.clone()).or(display0);
        let wayland_display = env.as_ref().and_then(|e| e.wayland.clone());
        // Need at least one path to the display server to show the prompt: X11
        // ($DISPLAY) or Wayland ($WAYLAND_DISPLAY). A pure-Wayland session has
        // only the latter.
        if display.is_none() && wayland_display.is_none() {
            return None;
        }
        let xauthority = env.as_ref().and_then(|e| e.xauthority.clone()).or_else(|| {
            let p = home.join(".Xauthority");
            p.is_file().then(|| p.to_string_lossy().into_owned())
        });
        let xdg_runtime_dir = env
            .as_ref()
            .and_then(|e| e.xdg.clone())
            .unwrap_or_else(|| format!("/run/user/{uid}"));
        Some(SessionCtx {
            uid,
            gid,
            user,
            home,
            display,
            wayland_display,
            xauthority,
            xdg_runtime_dir,
        })
    }

    /// Set the session environment on `cmd` and, at exec time, drop to the
    /// user (supplementary groups + gid + uid, in that order).
    pub fn apply(&self, cmd: &mut Command) {
        cmd.env("HOME", &self.home)
            .env("USER", &self.user)
            .env("LOGNAME", &self.user)
            .env("XDG_RUNTIME_DIR", &self.xdg_runtime_dir)
            .env(
                "PATH",
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            );
        // Give the child whatever the session actually has. GTK auto-selects its
        // backend, so on Wayland WAYLAND_DISPLAY is enough; on X11 DISPLAY +
        // XAUTHORITY. We deliberately don't force GDK_BACKEND.
        if let Some(d) = &self.display {
            cmd.env("DISPLAY", d);
        }
        if let Some(w) = &self.wayland_display {
            cmd.env("WAYLAND_DISPLAY", w);
        }
        if let Some(xauth) = &self.xauthority {
            cmd.env("XAUTHORITY", xauth);
        }
        let (uid, gid, user) = (self.uid, self.gid, self.user.clone());
        unsafe {
            cmd.pre_exec(move || {
                let c_user = std::ffi::CString::new(user.as_str())
                    .map_err(|_| std::io::Error::from_raw_os_error(libc::EINVAL))?;
                // initgroups needs to run while still root (it sets the whole
                // supplementary set, including `video` for the camera).
                if libc::initgroups(c_user.as_ptr(), gid as libc::gid_t) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if libc::setgid(gid as libc::gid_t) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if libc::setuid(uid as libc::uid_t) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
}

/// The active graphical session's `(uid, display)` via logind.
fn active_session() -> Option<(u32, Option<String>)> {
    // Preferred: the seat's currently active session.
    if let Some(id) = loginctl_value(&["show-seat", "seat0", "-p", "ActiveSession"]) {
        if !id.is_empty() {
            if let Some(s) = session_if_graphical(&id) {
                return Some(s);
            }
        }
    }
    // Fallback: scan every session for an active graphical one.
    let out = Command::new("loginctl")
        .args(["list-sessions", "--no-legend"])
        .output()
        .ok()?;
    for line in String::from_utf8_lossy(&out.stdout).lines() {
        if let Some(id) = line.split_whitespace().next() {
            if let Some(s) = session_if_graphical(id) {
                return Some(s);
            }
        }
    }
    None
}

/// `(uid, display)` if session `id` is an active x11/wayland session.
fn session_if_graphical(id: &str) -> Option<(u32, Option<String>)> {
    let out = Command::new("loginctl")
        .args(["show-session", id, "-p", "Active", "-p", "Type", "-p", "User", "-p", "Display"])
        .output()
        .ok()?;
    let (mut active, mut typ) = (false, String::new());
    let (mut uid, mut display) = (None, None);
    for l in String::from_utf8_lossy(&out.stdout).lines() {
        if let Some((k, v)) = l.split_once('=') {
            match k {
                "Active" => active = v == "yes",
                "Type" => typ = v.to_string(),
                "User" => uid = v.parse().ok(),
                "Display" if !v.is_empty() => display = Some(v.to_string()),
                _ => {}
            }
        }
    }
    if active && (typ == "x11" || typ == "wayland") {
        uid.map(|u| (u, display))
    } else {
        None
    }
}

fn loginctl_value(args: &[&str]) -> Option<String> {
    let out = Command::new("loginctl")
        .args(args)
        .arg("--value")
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn passwd(uid: u32) -> Option<(String, u32, PathBuf)> {
    unsafe {
        let pw = libc::getpwuid(uid as libc::uid_t);
        if pw.is_null() {
            return None;
        }
        let name = CStr::from_ptr((*pw).pw_name).to_string_lossy().into_owned();
        let gid = (*pw).pw_gid as u32;
        let home = PathBuf::from(CStr::from_ptr((*pw).pw_dir).to_string_lossy().into_owned());
        Some((name, gid, home))
    }
}

struct Env {
    display: Option<String>,
    wayland: Option<String>,
    xauthority: Option<String>,
    xdg: Option<String>,
}

/// Read `DISPLAY`/`WAYLAND_DISPLAY`/`XAUTHORITY`/`XDG_RUNTIME_DIR` from a live
/// process owned by `uid` (authoritative for the session's real display access).
/// Prefers a fully-formed session env: Wayland (has `WAYLAND_DISPLAY`) or X11
/// with its cookie (`DISPLAY` + `XAUTHORITY`).
fn scan_environ(uid: u32) -> Option<Env> {
    let mut best: Option<Env> = None;
    for e in fs::read_dir("/proc").ok()?.flatten() {
        let Some(pid) = e.file_name().to_str().and_then(|s| s.parse::<u32>().ok()) else {
            continue;
        };
        match e.metadata() {
            Ok(m) if m.uid() == uid => {}
            _ => continue,
        }
        let Ok(data) = fs::read(format!("/proc/{pid}/environ")) else {
            continue;
        };
        let mut env = Env { display: None, wayland: None, xauthority: None, xdg: None };
        for kv in data.split(|&b| b == 0) {
            if let Ok(s) = std::str::from_utf8(kv) {
                if let Some(v) = s.strip_prefix("DISPLAY=") {
                    env.display = Some(v.to_string());
                } else if let Some(v) = s.strip_prefix("WAYLAND_DISPLAY=") {
                    env.wayland = Some(v.to_string());
                } else if let Some(v) = s.strip_prefix("XAUTHORITY=") {
                    env.xauthority = Some(v.to_string());
                } else if let Some(v) = s.strip_prefix("XDG_RUNTIME_DIR=") {
                    env.xdg = Some(v.to_string());
                }
            }
        }
        if env.display.is_some() || env.wayland.is_some() {
            // Take a well-formed one immediately; otherwise keep as a fallback.
            if env.wayland.is_some() || env.xauthority.is_some() {
                return Some(env);
            }
            best = Some(env);
        }
    }
    best
}
