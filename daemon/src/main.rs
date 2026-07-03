//! AppLocker daemon — step 2.
//!
//! Step 1 proved the riskiest assumption: fanotify `FAN_OPEN_EXEC_PERM` can deny
//! an `execve` before the program runs. Step 2 turns that raw DENY into the real
//! **auth routine**: when a locked app is launched, the daemon runs
//! face(stub) → PIN / sudo-password prompt, and only then allows or denies.
//!
//! Subcommands:
//! ```text
//!   applockerd [TARGET]         gate mode: lock TARGET (substring of the binary
//!                               path, default "gnome-calculator"); needs root
//!   applockerd set-pin          set/replace the PIN fallback (writes the hash)
//!   applockerd config           show the current auth policy
//!   applockerd set-face on|off  enable/disable face (off = PIN/sudo only)
//!   applockerd set-fallback pin|sudo|both   choose which fallbacks are offered
//!   applockerd set-reauth session|always    cache unlock until lock, or re-ask
//!   applockerd list-installed   list installed apps (from .desktop files)
//!   applockerd lock-app <name>  add an app to the locked list
//!   applockerd unlock-app <q>   remove app(s) from the locked list
//!   applockerd list-apps        show the locked apps
//!   applockerd lock-folder <p>  lock a folder (file-gate)
//!   applockerd unlock-folder <q>  unlock a folder
//!   applockerd list-folders     show the locked folders
//!   applockerd auth-test        run the auth routine once and print Allowed/
//!                               Denied, with no fanotify — the easy way to test
//!                               the prompt, PIN and PAM without root
//! ```
//!
//! The gate handles a locked exec **asynchronously**: it hands the blocked
//! event's fd to a worker thread and keeps the main loop answering other execs.
//! This is what avoids the self-gating deadlock — spawning `python3` for the
//! prompt is itself an exec, and if we blocked the loop waiting for the prompt
//! we could never allow that python to start. See ../README.md.
//!
//! A **fail-open watchdog** (`$APPLOCKER_GATE_TIMEOUT`, default 30s) now bounds
//! how long a held exec waits on auth: if the recognizer/prompt hangs (busy
//! camera, no display), the gate ALLOWs rather than freezing that launch. And a
//! **test scope** (`$APPLOCKER_GATE_SCOPE`) marks a single mount instead of all
//! of `/`, so gate bugs can't wedge the whole machine while developing.
//!
//! The gate is now **exec-only** — it never marks `FAN_OPEN_PERM`. That
//! whole-`/` *open* gate used to intercept every file open, including the
//! event-loop thread's own config reads, which could self-deadlock and freeze
//! the machine. Files are protected by encrypted vaults (`vault.py`) instead.
//!
//! Still NOT here (later steps): multi-mount marks, D-Bus. Real system-wide
//! enforcement (`applocker on`) is validated in a throwaway VM, not the host.

use std::ffi::CString;
use std::fs;
use std::io::{self, BufRead, Write};
use std::time::Duration;
use std::mem;
use std::os::unix::io::RawFd;
use std::process;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::thread;

use applockerd::auth::{self, Outcome, SystemFallback};
use applockerd::desktop::{self, AppKind};
use applockerd::face;
use applockerd::feedback::{ClosingPrompter, FaceWithFeedback, Feedback};
use applockerd::folderlist::{self, FolderList, LockedFolder};
use applockerd::gate::{CachePolicy, Decision, GuiPrompter, UnlockCache};
use applockerd::locklist::{self, LockList, LockedApp};
use applockerd::pin;
use applockerd::policy;

/// Everything the gate matches against, reloaded together on SIGHUP.
struct Locks {
    apps: LockList,
    folders: FolderList,
}

/// Where the fanotify mark is applied. Production gates the whole filesystem
/// (`FAN_MARK_FILESYSTEM` on `/`); the dev-safe **test scope** gates a single
/// mount (`FAN_MARK_MOUNT`) so a bug can never freeze anything outside it.
#[derive(Clone)]
struct MarkSpec {
    /// Path to hand to `fanotify_mark`.
    path: String,
    /// true → whole filesystem (`/`); false → just this one mount (test scope).
    filesystem: bool,
}

/// Resolve the gate scope from `$APPLOCKER_GATE_SCOPE`. When set, the gate marks
/// only that mount instead of all of `/` — the safe way to test locking without
/// putting the whole machine behind the blocking permission gate.
///
/// The path MUST be its own mount point, or `FAN_MARK_MOUNT` would silently mark
/// the mount it *sits on* (often `/` — the exact disaster we're avoiding). We
/// refuse otherwise and tell the user how to make a scratch mount. Bypass the
/// check (at your own risk) with `APPLOCKER_GATE_FORCE=1`.
fn gate_scope() -> Option<MarkSpec> {
    let path = std::env::var("APPLOCKER_GATE_SCOPE").ok().filter(|s| !s.is_empty())?;
    let forced = std::env::var("APPLOCKER_GATE_FORCE").ok().as_deref() == Some("1");
    if !forced && !is_mount_point(&path) {
        eprintln!(
            "applockerd: APPLOCKER_GATE_SCOPE={path} is not its own mount point.\n\
             \x20 Marking it would gate the whole mount it sits on (likely /), which is\n\
             \x20 exactly the freeze we're avoiding. Make it a private mount first:\n\
             \x20   sudo mkdir -p {path} && sudo mount --bind {path} {path}\n\
             \x20 (a bind-mount to itself; undone by `sudo umount {path}` or a reboot.)\n\
             \x20 Or set APPLOCKER_GATE_FORCE=1 to override."
        );
        process::exit(1);
    }
    Some(MarkSpec { path, filesystem: false })
}

/// True if `path` is its own mount point. We consult `/proc/self/mountinfo`
/// rather than comparing `st_dev` with the parent: a **bind-mount to itself**
/// (how the test sandbox is made) keeps the underlying device number, so the
/// st_dev trick would wrongly report "not a mount". mountinfo lists every mount
/// point in field 5, which catches bind mounts correctly.
fn is_mount_point(path: &str) -> bool {
    // Canonicalise so "/tmp/applocker-test/" and symlinks compare equal to the
    // form the kernel records in mountinfo.
    let want = fs::canonicalize(path)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| path.to_string());
    let Ok(mi) = fs::read_to_string("/proc/self/mountinfo") else {
        return false;
    };
    // Each line: "id parent major:minor root MOUNTPOINT opts...". Field index 4
    // (0-based) is the mount point, with octal escapes for spaces etc.
    mi.lines().any(|line| {
        line.split_whitespace()
            .nth(4)
            .map(unescape_mountinfo)
            .is_some_and(|mp| mp == want)
    })
}

/// mountinfo escapes space/tab/newline/backslash as octal (`\040` etc.).
fn unescape_mountinfo(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut b = s.bytes();
    while let Some(c) = b.next() {
        if c == b'\\' {
            let digits: Vec<u8> = b.clone().take(3).collect();
            if digits.len() == 3 && digits.iter().all(|d| (b'0'..=b'7').contains(d)) {
                let val = (digits[0] - b'0') * 64 + (digits[1] - b'0') * 8 + (digits[2] - b'0');
                out.push(val as char);
                b.nth(2); // consume the three octal digits
                continue;
            }
        }
        out.push(c as char);
    }
    out
}

/// Dev mode: when `/etc/applocker/dev-mode` exists (or `$APPLOCKER_DEV=1`), the
/// gate refuses to mark the whole filesystem, so a bad gate can never freeze the
/// machine during testing. The dev-mode `.deb` ships this marker.
fn dev_mode() -> bool {
    std::env::var("APPLOCKER_DEV").ok().as_deref() == Some("1")
        || std::path::Path::new("/etc/applocker/dev-mode").exists()
}

/// Fail-open watchdog: if an auth run doesn't finish within this long, the gate
/// answers ALLOW and logs it, so a hung recognizer (busy camera, no display)
/// can't leave an exec blocked forever. `$APPLOCKER_GATE_TIMEOUT` seconds;
/// default 30; `0` disables (strict deny-until-answered, the old behaviour).
fn fail_open_timeout() -> Option<Duration> {
    let secs = std::env::var("APPLOCKER_GATE_TIMEOUT")
        .ok()
        .and_then(|s| s.trim().parse::<u64>().ok())
        .unwrap_or(30);
    (secs > 0).then(|| Duration::from_secs(secs))
}

// ── fanotify constants ──────────────────────────────────────────────────────
// Defined locally rather than relying on libc exposing every one of them, so
// the spike builds against an older libc as long as the *kernel* is new enough.

const FAN_CLOEXEC: libc::c_uint = 0x0000_0001;
const FAN_CLASS_CONTENT: libc::c_uint = 0x0000_0004; // required for PERM events

const FAN_MARK_ADD: libc::c_uint = 0x0000_0001;
const FAN_MARK_MOUNT: libc::c_uint = 0x0000_0010; // gate one mount only (test scope)
const FAN_MARK_FILESYSTEM: libc::c_uint = 0x0000_0100;

const FAN_OPEN_PERM: u64 = 0x0001_0000; // regular file opens (the file-gate)
const FAN_OPEN_EXEC_PERM: u64 = 0x0004_0000; // execs (the app-gate)

const FAN_ALLOW: u32 = 0x01;
const FAN_DENY: u32 = 0x02;

const FANOTIFY_METADATA_VERSION: u8 = 3;

/// The kernel's reply struct for a permission event: `{ s32 fd; u32 response; }`.
#[repr(C)]
struct FanotifyResponse {
    fd: libc::c_int,
    response: u32,
}

fn main() {
    // Restore default SIGPIPE so piping our output into `head` etc. exits
    // quietly instead of panicking on a broken pipe (Rust ignores SIGPIPE).
    unsafe { libc::signal(libc::SIGPIPE, libc::SIG_DFL) };

    let arg = std::env::args().nth(1);
    match arg.as_deref() {
        Some("set-pin") => cmd_set_pin(),
        Some("config") => cmd_config_show(),
        Some("set-face") => cmd_set_face(std::env::args().nth(2)),
        Some("session-probe") => cmd_session_probe(),
        Some("set-attention") => cmd_set_attention(std::env::args().nth(2)),
        Some("set-attention-interval") => cmd_set_attention_interval(std::env::args().nth(2)),
        Some("set-attention-ac-only") => cmd_set_attention_ac_only(std::env::args().nth(2)),
        Some("set-fallback") => cmd_set_fallback(std::env::args().nth(2)),
        Some("set-reauth") => cmd_set_reauth(std::env::args().nth(2)),
        Some("authorize") => cmd_auth_test(std::env::args().nth(2)), // alias for GUI gating
        Some("list-installed") => cmd_list_installed(),
        Some("list-apps") => cmd_list_apps(),
        Some("lock-app") => cmd_lock_app(std::env::args().nth(2)),
        Some("unlock-app") => cmd_unlock_app(std::env::args().nth(2)),
        Some("list-folders") => cmd_list_folders(),
        Some("lock-folder") => cmd_lock_folder(std::env::args().nth(2)),
        Some("unlock-folder") => cmd_unlock_folder(std::env::args().nth(2)),
        Some("auth-test") => cmd_auth_test(std::env::args().nth(2)),
        Some("gate") | None => cmd_gate(None),
        // Back-compat: `applockerd <name>` gates that one app for this run,
        // on top of the persisted locked list.
        Some(other) => cmd_gate(Some(other.to_string())),
    }
}

// ── config / policy ───────────────────────────────────────────────────────────

fn cmd_config_show() {
    let path = policy::default_path();
    let pol = policy::Policy::load(&path);
    println!("AppLocker auth policy ({}):", path.display());
    println!("  {}", pol.summary());
    println!(
        "  PIN is {}set",
        if pin::is_set(&auth::default_pin_path()) { "" } else { "NOT " }
    );
    println!("\nChange with:");
    println!("  applockerd set-face on|off");
    println!("  applockerd set-attention on|off");
    println!("  applockerd set-attention-interval 2|5|10|15|30   (minutes between checks)");
    println!("  applockerd set-attention-ac-only on|off          (pause on battery)");
    println!("  applockerd set-fallback pin|sudo|both");
    println!("  applockerd set-reauth session|always");
}

fn cmd_set_face(arg: Option<String>) {
    let on = match arg.as_deref() {
        Some("on") => true,
        Some("off") => false,
        _ => {
            eprintln!("usage: applockerd set-face on|off");
            process::exit(2);
        }
    };
    let path = policy::default_path();
    let mut pol = policy::Policy::load(&path);
    pol.face_enabled = on;
    save_policy_or_exit(&pol, &path);
    println!("face {} — {}", if on { "enabled" } else { "disabled" }, pol.summary());
    if on && !pin::is_set(&auth::default_pin_path()) {
        eprintln!("note: face still needs enrollment (face/enroll.py) to actually run.");
    }
}

/// Debug: show which graphical session the root daemon would drop prompts into.
/// Run as root (`sudo applockerd session-probe`) to see the real result.
fn cmd_session_probe() {
    let euid = unsafe { libc::geteuid() };
    println!("euid: {euid} ({})", if euid == 0 { "root" } else { "not root — run with sudo" });
    match applockerd::session::SessionCtx::discover() {
        Some(c) => {
            println!("active session found:");
            println!("  user:            {} (uid {}, gid {})", c.user, c.uid, c.gid);
            println!("  home:            {}", c.home.display());
            println!("  DISPLAY:         {}", c.display.as_deref().unwrap_or("(none — Wayland?)"));
            println!("  WAYLAND_DISPLAY: {}", c.wayland_display.as_deref().unwrap_or("(none)"));
            println!("  XAUTHORITY:      {}", c.xauthority.as_deref().unwrap_or("(none found)"));
            println!("  XDG_RUNTIME_DIR: {}", c.xdg_runtime_dir);
            let kind = if c.wayland_display.is_some() { "Wayland" } else { "X11" };
            println!("\nGUI prompts + the camera recognizer will run as this user ({kind}).");
        }
        None => println!(
            "no active graphical session found — prompts would inherit the daemon's \
             environment (fine for `sudo applockerd` in your session; a boot service \
             would have no DISPLAY until someone logs in)."
        ),
    }
}

fn cmd_set_attention(arg: Option<String>) {
    let on = match arg.as_deref() {
        Some("on") => true,
        Some("off") => false,
        _ => {
            eprintln!("usage: applockerd set-attention on|off");
            process::exit(2);
        }
    };
    let path = policy::default_path();
    let mut pol = policy::Policy::load(&path);
    pol.attention_enabled = on;
    save_policy_or_exit(&pol, &path);
    println!(
        "attention {} — {}",
        if on { "enabled" } else { "disabled" },
        pol.summary()
    );
    if on {
        println!("start the watcher in your session: python3 face/watch_presence.py");
    }
}

fn cmd_set_attention_interval(arg: Option<String>) {
    let min = match arg.as_deref().and_then(|s| s.parse::<u32>().ok()) {
        Some(n) if policy::ATTENTION_INTERVALS.contains(&n) => n,
        _ => {
            eprintln!("usage: applockerd set-attention-interval 2|5|10|15|30");
            process::exit(2);
        }
    };
    let path = policy::default_path();
    let mut pol = policy::Policy::load(&path);
    pol.attention_interval_min = min;
    save_policy_or_exit(&pol, &path);
    println!("attention interval {}m — {}", min, pol.summary());
}

fn cmd_set_attention_ac_only(arg: Option<String>) {
    let on = match arg.as_deref() {
        Some("on") => true,
        Some("off") => false,
        _ => {
            eprintln!("usage: applockerd set-attention-ac-only on|off");
            process::exit(2);
        }
    };
    let path = policy::default_path();
    let mut pol = policy::Policy::load(&path);
    pol.attention_ac_only = on;
    save_policy_or_exit(&pol, &path);
    println!(
        "attention AC-only {} — {}",
        if on { "enabled" } else { "disabled" },
        pol.summary()
    );
}

fn cmd_set_fallback(arg: Option<String>) {
    let (pin_on, sudo_on) = match arg.as_deref() {
        Some("pin") => (true, false),
        Some("sudo") | Some("password") => (false, true),
        Some("both") | Some("all") | Some("pin,sudo") | Some("sudo,pin") => (true, true),
        _ => {
            eprintln!("usage: applockerd set-fallback pin|sudo|both");
            process::exit(2);
        }
    };
    let path = policy::default_path();
    let mut pol = policy::Policy::load(&path);
    pol.allow_pin = pin_on;
    pol.allow_sudo = sudo_on;
    save_policy_or_exit(&pol, &path);
    println!("{}", pol.summary());
    // Warn about a foot-gun: PIN-only with no PIN set is a lock-out.
    if pin_on && !sudo_on && !pin::is_set(&auth::default_pin_path()) {
        eprintln!("WARNING: PIN-only selected but no PIN is set — run `applockerd set-pin` \
                   now or you'll be locked out of locked apps.");
    }
}

fn cmd_set_reauth(arg: Option<String>) {
    let every = match arg.as_deref() {
        Some("session") | Some("once") => false,
        Some("always") | Some("every") => true,
        _ => {
            eprintln!("usage: applockerd set-reauth session|always");
            eprintln!("  session = ask once, then not again until the screen locks");
            eprintln!("  always  = ask every time a locked app launches");
            process::exit(2);
        }
    };
    let path = policy::default_path();
    let mut pol = policy::Policy::load(&path);
    pol.reauth_every_time = every;
    save_policy_or_exit(&pol, &path);
    println!("{}", pol.summary());
}

fn save_policy_or_exit(pol: &policy::Policy, path: &std::path::Path) {
    if let Err(e) = pol.save(path) {
        eprintln!("applockerd: could not write {}: {e}", path.display());
        eprintln!("(the system config path needs root; or set APPLOCKER_CONFIG)");
        process::exit(1);
    }
}

// ── set-pin ───────────────────────────────────────────────────────────────────

fn cmd_set_pin() {
    let path = auth::default_pin_path();
    println!("Setting AppLocker PIN at {}", path.display());
    let pin1 = match read_secret("New PIN: ") {
        Ok(p) => p,
        Err(e) => {
            eprintln!("applockerd: {e}");
            process::exit(1);
        }
    };
    if pin1.trim().is_empty() {
        eprintln!("applockerd: empty PIN rejected");
        process::exit(1);
    }
    let pin2 = read_secret("Confirm PIN: ").unwrap_or_default();
    if pin1 != pin2 {
        eprintln!("applockerd: PINs did not match");
        process::exit(1);
    }
    match pin::set_pin(&path, &pin1) {
        Ok(()) => println!("PIN set."),
        Err(e) => {
            eprintln!("applockerd: could not write {}: {e}", path.display());
            eprintln!("(the system PIN path needs root; or set APPLOCKER_PIN_FILE)");
            process::exit(1);
        }
    }
}

/// Read a line from the terminal with echo disabled (so the PIN isn't shown).
/// Falls back to echoed input if stdin isn't a tty.
fn read_secret(prompt: &str) -> io::Result<String> {
    print!("{prompt}");
    io::stdout().flush()?;

    let fd = libc::STDIN_FILENO;
    let is_tty = unsafe { libc::isatty(fd) } == 1;

    let mut saved: libc::termios = unsafe { mem::zeroed() };
    if is_tty {
        unsafe {
            libc::tcgetattr(fd, &mut saved);
            let mut raw = saved;
            raw.c_lflag &= !libc::ECHO;
            libc::tcsetattr(fd, libc::TCSANOW, &raw);
        }
    }

    let mut line = String::new();
    let read_res = io::stdin().read_line(&mut line);

    if is_tty {
        unsafe { libc::tcsetattr(fd, libc::TCSANOW, &saved) };
        println!(); // the newline the user's Enter didn't echo
    }
    read_res?;
    Ok(line.trim_end_matches(['\n', '\r']).to_string())
}

// ── locked-apps management ──────────────────────────────────────────────────

fn cmd_list_installed() {
    let apps = desktop::installed();
    // `--porcelain` emits stable `kind<TAB>key<TAB>name` for the GUI to parse.
    if std::env::args().any(|a| a == "--porcelain") {
        for a in &apps {
            println!("{}\t{}\t{}", a.kind.as_str(), a.key, a.name);
        }
        return;
    }
    println!("{} installed apps:", apps.len());
    for a in &apps {
        let flag = if a.kind.is_gateable() { "" } else { "  (not gateable yet)" };
        println!("  {:<34} {} [{}]{}", a.name, a.key, a.kind.as_str(), flag);
    }
}

fn cmd_list_apps() {
    let path = locklist::default_path();
    let list = locklist::LockList::load(&path);
    if list.apps.is_empty() {
        println!("No apps locked. Add one with: applockerd lock-app <name>");
        return;
    }
    println!("Locked apps ({}):", path.display());
    for a in &list.apps {
        let flag = if a.kind.is_gateable() { "" } else { "  (stored; not gateable yet)" };
        println!("  {:<26} {} [{}]{}", a.name, a.key, a.kind.as_str(), flag);
    }
}

/// Find an installed app by desktop id, name, or key (the value `list-installed`
/// prints) — case-insensitive — and add it to the locked list.
fn cmd_lock_app(query: Option<String>) {
    let Some(query) = query else {
        eprintln!("usage: applockerd lock-app <app name, key, or desktop id>");
        process::exit(2);
    };
    let q = query.to_lowercase();
    let installed = desktop::installed();
    let hits: Vec<_> = installed
        .iter()
        .filter(|a| {
            a.id.to_lowercase() == q
                || a.key.to_lowercase() == q
                || desktop::base(&a.key).to_lowercase() == q
                || a.name.to_lowercase().contains(&q)
        })
        .collect();

    let app = match hits.len() {
        0 => {
            eprintln!("no installed app matches {query:?}. Try: applockerd list-installed");
            process::exit(1);
        }
        1 => hits[0],
        _ => {
            // Prefer an exact name match if the substring hit several.
            match hits.iter().find(|a| a.name.to_lowercase() == q) {
                Some(exact) => exact,
                None => {
                    eprintln!("{query:?} matches several apps — be more specific:");
                    for a in hits {
                        eprintln!("  {} ({})", a.name, a.id);
                    }
                    process::exit(1);
                }
            }
        }
    };

    let path = locklist::default_path();
    let mut list = locklist::LockList::load(&path);
    if list.add(locklist::LockedApp::from_desktop(app)) {
        save_locklist_or_exit(&list, &path);
        println!("locked {:?} ({} [{}])", app.name, app.key, app.kind.as_str());
        if !app.kind.is_gateable() {
            eprintln!(
                "note: {} apps aren't gateable by binary path yet, so this lock is \
                 stored but not yet enforced.",
                app.kind.as_str()
            );
        }
        signal_daemon_reload();
    } else {
        println!("{:?} is already locked.", app.name);
    }
}

fn cmd_unlock_app(query: Option<String>) {
    let Some(query) = query else {
        eprintln!("usage: applockerd unlock-app <app name or key>");
        process::exit(2);
    };
    let path = locklist::default_path();
    let mut list = locklist::LockList::load(&path);
    let n = list.remove(&query);
    if n == 0 {
        eprintln!("nothing matched {query:?} in the locked list.");
        process::exit(1);
    }
    save_locklist_or_exit(&list, &path);
    println!("unlocked {n} app(s) matching {query:?}.");
    signal_daemon_reload();
}

fn save_locklist_or_exit(list: &locklist::LockList, path: &std::path::Path) {
    if let Err(e) = list.save(path) {
        eprintln!("applockerd: could not write {}: {e}", path.display());
        eprintln!("(the system path needs root; or set APPLOCKER_LOCKED_APPS)");
        process::exit(1);
    }
}

// ── locked-folders management ───────────────────────────────────────────────

fn cmd_list_folders() {
    let path = folderlist::default_path();
    let list = FolderList::load(&path);
    if list.is_empty() {
        println!("No folders locked. Add one with: applockerd lock-folder <path>");
        return;
    }
    println!("Locked folders ({}):", path.display());
    for f in &list.folders {
        println!("  {:<24} {}", f.name, f.path);
    }
}

fn cmd_lock_folder(arg: Option<String>) {
    let Some(input) = arg else {
        eprintln!("usage: applockerd lock-folder <path>");
        process::exit(2);
    };
    let canon = match folderlist::is_safe_to_lock(&input) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("applockerd: {e}");
            process::exit(1);
        }
    };
    let name = std::path::Path::new(&canon)
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| canon.clone());

    let path = folderlist::default_path();
    let mut list = FolderList::load(&path);
    if list.add(LockedFolder { path: canon.clone(), name: name.clone() }) {
        save_folderlist_or_exit(&list, &path);
        println!("locked folder {name:?} ({canon})");
        signal_daemon_reload();
    } else {
        println!("{canon} is already locked.");
    }
}

fn cmd_unlock_folder(arg: Option<String>) {
    let Some(query) = arg else {
        eprintln!("usage: applockerd unlock-folder <path or name>");
        process::exit(2);
    };
    // Accept either the exact stored (canonical) path or a name; canonicalise the
    // input too so `unlock-folder ./Documents` matches the stored absolute path.
    let path = folderlist::default_path();
    let mut list = FolderList::load(&path);
    let mut n = list.remove(&query);
    if n == 0 {
        if let Ok(canon) = std::fs::canonicalize(&query) {
            n = list.remove(&canon.to_string_lossy());
        }
    }
    if n == 0 {
        eprintln!("nothing matched {query:?} in the locked folders.");
        process::exit(1);
    }
    save_folderlist_or_exit(&list, &path);
    println!("unlocked {n} folder(s) matching {query:?}.");
    signal_daemon_reload();
}

fn save_folderlist_or_exit(list: &FolderList, path: &std::path::Path) {
    if let Err(e) = list.save(path) {
        eprintln!("applockerd: could not write {}: {e}", path.display());
        eprintln!("(the system path needs root; or set APPLOCKER_LOCKED_FOLDERS)");
        process::exit(1);
    }
}

/// Ask a running gate daemon to reload the locked list, by SIGHUP to any other
/// `applockerd` process. Best-effort — if none is running, changes apply on next
/// start. We never signal ourselves.
fn signal_daemon_reload() {
    let me = process::id();
    let Ok(entries) = fs::read_dir("/proc") else { return };
    for e in entries.flatten() {
        let Ok(pid) = e.file_name().to_string_lossy().parse::<i32>() else { continue };
        if pid as u32 == me {
            continue;
        }
        let comm = fs::read_to_string(format!("/proc/{pid}/comm")).unwrap_or_default();
        if comm.trim() == "applockerd" {
            unsafe { libc::kill(pid, libc::SIGHUP) };
        }
    }
}

// ── auth-test ─────────────────────────────────────────────────────────────────

/// Run the auth routine once, no fanotify. The quickest way to exercise the
/// prompt + PIN + PAM end to end: `applockerd auth-test [app-name]`.
fn cmd_auth_test(app: Option<String>) {
    let app = app.unwrap_or_else(|| "auth-test".to_string());
    let (face, attempts, face_live) = face::build();
    let cfg = face::config_for(attempts);
    let fb = std::rc::Rc::new(std::cell::RefCell::new(if face_live {
        Feedback::spawn(&app)
    } else {
        Feedback::none()
    }));
    let mut face = FaceWithFeedback::new(face, fb.clone(), attempts);
    let mut prompter = ClosingPrompter::new(GuiPrompter::new(&app, face_live), fb);
    let fallback = SystemFallback::system();
    let pol = policy::load_default();

    println!(
        "applockerd: auth-test for {app:?} (user={}) — policy: {}",
        fallback.user,
        pol.summary(),
    );

    match auth::run(&cfg, &mut face, &mut prompter, &fallback) {
        Outcome::Allowed => println!("ALLOWED"),
        Outcome::Denied => {
            println!("DENIED");
            process::exit(1);
        }
    }
}

// ── gate mode ─────────────────────────────────────────────────────────────────

/// Set by the SIGHUP handler; the event loop reloads the locked list when it's
/// seen (SIGHUP interrupts the blocking read with EINTR).
static RELOAD_LOCKS: AtomicBool = AtomicBool::new(false);

extern "C" fn on_sighup(_sig: libc::c_int) {
    RELOAD_LOCKS.store(true, Ordering::SeqCst);
}

fn cmd_gate(adhoc: Option<String>) {
    if unsafe { libc::geteuid() } != 0 {
        eprintln!("applockerd: gate mode must run as root (try: sudo applockerd)");
        process::exit(1);
    }

    // Load both lists, plus any ad-hoc app named on the CLI.
    let mut apps = LockList::load(&locklist::default_path());
    if let Some(name) = adhoc {
        apps.add(LockedApp { kind: AppKind::Native, key: name.clone(), name });
    }
    // Folder locking is handled by encrypted vaults (vault.py), NOT fanotify, so
    // we keep an empty folder list — the file-gate is never engaged.
    let folders = FolderList::default();
    let n_apps = apps.apps.len();
    let legacy_folders = FolderList::load(&folderlist::default_path()).folders.len();
    if legacy_folders > 0 {
        eprintln!("applockerd: note: {legacy_folders} legacy locked-folder(s) IGNORED — \
                   folder locking now uses encrypted vaults (vault.py / Settings).");
    }
    if n_apps == 0 {
        eprintln!("applockerd: no apps locked yet — add with lock-app.");
    }
    let locks = Arc::new(RwLock::new(Locks { apps, folders }));

    let fan_fd = init_fanotify().unwrap_or_else(|e| {
        eprintln!("applockerd: fanotify_init failed: {e}");
        process::exit(1);
    });

    // Exec-only gate. We never mark FAN_OPEN_PERM: that whole-filesystem *open*
    // gate intercepted EVERY file open — including the daemon's own config reads
    // on this very event-loop thread — which could self-deadlock and freeze the
    // machine. Files are protected by encrypted vaults instead.
    let mask = FAN_OPEN_EXEC_PERM;
    // Test scope (a single mount) if $APPLOCKER_GATE_SCOPE is set, else all of /.
    let mark = gate_scope().unwrap_or(MarkSpec { path: "/".into(), filesystem: true });
    // Dev mode: refuse to gate the whole filesystem. This makes the input-freeze
    // impossible no matter what starts the gate (`applocker on`, the service) —
    // the app-gate only runs when explicitly scoped to a sandbox mount. Vaults,
    // face and the GUI are unaffected. Turn off by deleting /etc/applocker/dev-mode.
    if dev_mode() && mark.filesystem {
        eprintln!("applockerd: DEV MODE — not gating all of / (freeze risk); doing nothing.");
        eprintln!("applockerd: set APPLOCKER_GATE_SCOPE=<a private mount> to test the");
        eprintln!("applockerd: app-gate safely (see packaging/bin/applocker-test-scope),");
        eprintln!("applockerd: or remove /etc/applocker/dev-mode to allow the real gate.");
        // Exit 0, not 1: a clean no-op. Exiting non-zero here made the systemd
        // service (Restart=on-failure) restart-storm and land in `failed` when
        // someone started it in dev mode. A clean exit just stops.
        process::exit(0);
    }
    if let Err(e) = mark_scope(fan_fd, &mark, mask) {
        eprintln!("applockerd: fanotify_mark failed: {e}");
        process::exit(1);
    }

    // Reload both lists live on SIGHUP (lock-*/unlock-* send it).
    unsafe { libc::signal(libc::SIGHUP, on_sighup as *const () as libc::sighandler_t) };

    let fail_open = fail_open_timeout();
    if mark.filesystem {
        eprintln!("applockerd: gating {n_apps} app(s) on / (WHOLE SYSTEM, exec-only).");
    } else {
        eprintln!("applockerd: *** TEST SCOPE *** gating only the '{}' mount — the rest of", mark.path);
        eprintln!("applockerd: the system is NOT gated and cannot freeze. {n_apps} app(s).");
    }
    match fail_open {
        Some(d) => eprintln!("applockerd: fail-open watchdog: auth hangs auto-ALLOW after {}s.", d.as_secs()),
        None => eprintln!("applockerd: fail-open watchdog DISABLED — a hung auth blocks that launch forever."),
    }
    eprintln!("applockerd: policy: {}", policy::load_default().summary());
    eprintln!("applockerd: (PIN set: {})", pin::is_set(&auth::default_pin_path()));
    eprintln!("applockerd: Ctrl-C to stop; SIGHUP reloads the lists.");

    // Shared across worker threads: the unlock cache and a lock serialising the
    // fixed-size response writes to the fanotify fd.
    let cache = Arc::new(UnlockCache::new());
    let write_lock = Arc::new(Mutex::new(()));

    // Re-lock everything when the session locks or the machine sleeps: manual
    // lock, lid-close, and the attention watcher's `loginctl lock-session` all
    // funnel through logind, so this one listener covers them all.
    spawn_lock_listener(Arc::clone(&cache));

    event_loop(fan_fd, locks, cache, write_lock, fail_open);
}

/// Watch logind for `Session.Lock` / `PrepareForSleep(true)` signals and wipe
/// the unlock cache when they fire. Uses `gdbus monitor` (ships with GLib) so
/// the std-only daemon needs no D-Bus crate; if gdbus is missing we log it and
/// the cache simply lives until the daemon restarts (previous behaviour).
fn spawn_lock_listener(cache: Arc<UnlockCache>) {
    thread::spawn(move || loop {
        let child = process::Command::new("gdbus")
            .args(["monitor", "-y", "-d", "org.freedesktop.login1"])
            .stdout(process::Stdio::piped())
            .stderr(process::Stdio::null())
            .spawn();
        let mut child = match child {
            Ok(c) => c,
            Err(e) => {
                eprintln!("applockerd: no lock listener (gdbus unavailable: {e}) — \
                           unlocks last until the daemon restarts.");
                return;
            }
        };
        if let Some(stdout) = child.stdout.take() {
            let reader = io::BufReader::new(stdout);
            for line in reader.lines() {
                let Ok(line) = line else { break };
                if line.contains(".Session.Lock (")
                    || line.contains("PrepareForSleep (true")
                {
                    cache.wipe();
                    eprintln!("applockerd: session locked — unlock cache wiped.");
                }
            }
        }
        let _ = child.wait();
        // gdbus died (session bus restart?) — retry after a beat.
        thread::sleep(Duration::from_secs(5));
    });
}

fn init_fanotify() -> std::io::Result<RawFd> {
    // FAN_CLASS_CONTENT is what unlocks permission (allow/deny) events.
    // event_f_flags = how the kernel opens the fd it hands us (read-only is enough).
    let fd = unsafe {
        libc::fanotify_init(FAN_CLOEXEC | FAN_CLASS_CONTENT, libc::O_RDONLY as libc::c_uint)
    };
    if fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(fd)
}

fn mark_scope(fan_fd: RawFd, spec: &MarkSpec, mask: u64) -> std::io::Result<()> {
    // FAN_MARK_FILESYSTEM covers the whole filesystem containing `path`, so any
    // exec (and, if requested, any open) on that fs generates an event. (A
    // separate /home or flatpak store is a different fs and would need its own
    // mark — noted in README as a known gap.) FAN_MARK_MOUNT instead gates just
    // one mount — the test scope, so a bug can't freeze the rest of the machine.
    let flag = if spec.filesystem { FAN_MARK_FILESYSTEM } else { FAN_MARK_MOUNT };
    let c_path = CString::new(spec.path.as_str()).unwrap();
    let rc = unsafe {
        libc::fanotify_mark(
            fan_fd,
            FAN_MARK_ADD | flag,
            mask,
            libc::AT_FDCWD,
            c_path.as_ptr(),
        )
    };
    if rc < 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

fn event_loop(
    fan_fd: RawFd,
    locks: Arc<RwLock<Locks>>,
    cache: Arc<UnlockCache>,
    write_lock: Arc<Mutex<()>>,
    fail_open: Option<Duration>,
) {
    let mut buf = [0u8; 8192];
    let meta_size = mem::size_of::<libc::fanotify_event_metadata>();

    loop {
        // Apply a pending SIGHUP reload before blocking again.
        if RELOAD_LOCKS.swap(false, Ordering::SeqCst) {
            let apps = LockList::load(&locklist::default_path());
            let na = apps.apps.len();
            // Exec-only: never (re-)mark FAN_OPEN_PERM. Folders use vaults.
            *locks.write().unwrap() = Locks { apps, folders: FolderList::default() };
            eprintln!("applockerd: reloaded locked apps ({na}).");
        }

        let len = unsafe {
            libc::read(fan_fd, buf.as_mut_ptr() as *mut libc::c_void, buf.len())
        };
        if len < 0 {
            let err = std::io::Error::last_os_error();
            if err.raw_os_error() == Some(libc::EINTR) {
                continue; // likely our SIGHUP — loop back and reload
            }
            eprintln!("applockerd: read error: {err}");
            return;
        }
        if len == 0 {
            continue;
        }

        let mut offset = 0usize;
        let len = len as usize;
        while len - offset >= meta_size {
            // SAFETY: we just confirmed at least one metadata struct remains.
            let meta = unsafe {
                &*(buf.as_ptr().add(offset) as *const libc::fanotify_event_metadata)
            };

            if meta.vers != FANOTIFY_METADATA_VERSION {
                eprintln!(
                    "applockerd: metadata version mismatch (got {}, expected {}) — aborting",
                    meta.vers, FANOTIFY_METADATA_VERSION
                );
                return;
            }
            if meta.event_len == 0 {
                break;
            }

            if meta.fd >= 0 {
                // handle_event returns true if it took ownership of the event fd
                // (deferred to a worker thread); if so, we must NOT close it here.
                let deferred = handle_event(
                    fan_fd,
                    meta.fd,
                    meta.mask,
                    meta.pid,
                    &locks,
                    &cache,
                    &write_lock,
                    fail_open,
                );
                if !deferred {
                    unsafe { libc::close(meta.fd) };
                }
            }

            offset += meta.event_len as usize;
        }
    }
}

/// Decide an exec or open event. Returns `true` if the event fd was handed to a
/// worker thread (the caller must then not close it).
fn handle_event(
    fan_fd: RawFd,
    event_fd: libc::c_int,
    mask: u64,
    pid: libc::c_int,
    locks: &Arc<RwLock<Locks>>,
    cache: &Arc<UnlockCache>,
    write_lock: &Arc<Mutex<()>>,
    fail_open: Option<Duration>,
) -> bool {
    let is_exec = mask & FAN_OPEN_EXEC_PERM != 0;
    let is_open = mask & FAN_OPEN_PERM != 0;
    if !is_exec && !is_open {
        return false;
    }

    let path = fs::read_link(format!("/proc/self/fd/{event_fd}"))
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| "<unknown>".to_string());

    // Resolve which lock (if any) this event hits: an exec checks the app list by
    // binary, a file open checks the folder list by path prefix. We copy out the
    // (key, name) and drop the read lock before any slow work. Not locked → allow
    // inline — this includes the python3/PAM opens our own prompt makes, so no
    // self-gating deadlock (the loop keeps answering while a worker runs auth).
    let hit: Option<(String, String)> = {
        let guard = locks.read().unwrap();
        if is_exec {
            guard.apps.matches(&path).map(|a| (a.key.clone(), a.name.clone()))
        } else {
            guard.folders.matches(&path).map(|f| (f.path.clone(), f.name.clone()))
        }
    };
    let Some((target, app_name)) = hit else {
        respond(fan_fd, event_fd, FAN_ALLOW, write_lock);
        return false;
    };

    // Never gate our own helpers. The auth stack (recognize.py, the prompt, the
    // feedback window) runs as our children and opens scripts/enrollment files —
    // if those live under a locked folder, gating them deadlocks the unlock
    // itself. A descendant of the daemon is always allowed through.
    if pid_is_our_descendant(pid) {
        respond(fan_fd, event_fd, FAN_ALLOW, write_lock);
        println!("allow  pid={pid:<7} {app_name} ({path}, own helper)");
        return false;
    }

    // Re-read the policy per locked exec so changes apply live.
    let cache_policy = if policy::load_default().reauth_every_time {
        CachePolicy::EveryTime
    } else {
        CachePolicy::OncePerSession
    };

    match cache.begin(&target, cache_policy) {
        Decision::AlreadyUnlocked => {
            respond(fan_fd, event_fd, FAN_ALLOW, write_lock);
            println!("allow  pid={pid:<7} {app_name} ({path}, cached)");
            false
        }
        Decision::PromptInFlight => {
            // A prompt is already open; don't stack another. Deny this attempt.
            respond(fan_fd, event_fd, FAN_DENY, write_lock);
            println!("DENY   pid={pid:<7} {app_name} (prompt already open)");
            false
        }
        Decision::NeedsAuth => {
            // Defer: run the (blocking, interactive) auth routine off the event
            // loop, then respond for this held event. Ownership of event_fd
            // moves into the thread, which closes it when done.
            let cache = Arc::clone(cache);
            let write_lock = Arc::clone(write_lock);
            let app = app_name.clone();
            println!("auth   pid={pid:<7} {app_name} (prompting)");

            thread::spawn(move || {
                // The actual auth (camera, GTK prompt) runs in an *inner* thread
                // and reports its verdict over a channel. The outer thread waits
                // with the fail-open deadline: if auth doesn't answer in time it
                // ALLOWs anyway, so a hung recognizer (busy camera, no display on
                // Wayland-as-root) can never leave this exec blocked forever.
                // Only the outer thread ever replies, so there's no double-answer
                // race even when the inner thread finishes late.
                let (tx, rx) = std::sync::mpsc::channel::<bool>();
                let app_inner = app.clone();
                thread::spawn(move || {
                    let (face, attempts, face_live) = face::build();
                    let cfg = face::config_for(attempts);
                    let fb = std::rc::Rc::new(std::cell::RefCell::new(if face_live {
                        Feedback::spawn(&app_inner)
                    } else {
                        Feedback::none()
                    }));
                    let mut face = FaceWithFeedback::new(face, fb.clone(), attempts);
                    let mut prompter = ClosingPrompter::new(GuiPrompter::new(&app_inner, face_live), fb);
                    let fallback = SystemFallback::system();
                    let outcome = auth::run(&cfg, &mut face, &mut prompter, &fallback);
                    // If the outer thread already timed out, the receiver is gone
                    // and this send is a harmless no-op.
                    let _ = tx.send(matches!(outcome, Outcome::Allowed));
                });

                let (allowed, timed_out) = match fail_open {
                    Some(d) => match rx.recv_timeout(d) {
                        Ok(a) => (a, false),
                        Err(_) => (true, true), // fail-open: allow rather than freeze
                    },
                    None => (rx.recv().unwrap_or(false), false),
                };

                // Never cache a fail-open allow — it's a safety escape, not a real
                // unlock; the next launch should prompt again.
                cache.finish(&target, allowed && !timed_out, cache_policy);
                respond(
                    fan_fd,
                    event_fd,
                    if allowed { FAN_ALLOW } else { FAN_DENY },
                    &write_lock,
                );
                unsafe { libc::close(event_fd) };
                let verdict = if timed_out {
                    "ALLOW (fail-open: auth timed out)"
                } else if allowed {
                    "allow (auth passed)"
                } else {
                    "DENY (auth failed)"
                };
                println!("{verdict:<34} pid={pid:<7} {path}");
            });
            true
        }
    }
}

/// Is `pid` this daemon or one of its descendants? Walks the PPid chain in
/// /proc (a handful of small reads; only runs for events that hit a lock). A
/// vanished process reads as "not ours" — fail closed to the normal auth path.
fn pid_is_our_descendant(pid: libc::c_int) -> bool {
    let me = std::process::id() as libc::c_int;
    let mut cur = pid;
    for _ in 0..64 {
        if cur == me {
            return true;
        }
        if cur <= 1 {
            return false;
        }
        // /proc/<pid>/stat: "pid (comm) state ppid ..." — comm may contain
        // spaces/parens, so parse after the LAST ')'.
        let stat = match fs::read_to_string(format!("/proc/{cur}/stat")) {
            Ok(s) => s,
            Err(_) => return false,
        };
        let after = match stat.rfind(')') {
            Some(i) => &stat[i + 1..],
            None => return false,
        };
        cur = match after.split_whitespace().nth(1).and_then(|s| s.parse().ok()) {
            Some(p) => p,
            None => return false,
        };
    }
    false
}

fn respond(fan_fd: RawFd, event_fd: libc::c_int, response: u32, write_lock: &Arc<Mutex<()>>) {
    // CRITICAL: every permission event MUST get exactly one reply, or the
    // execing process hangs blocked in the kernel until this fd closes. The lock
    // serialises the fixed-size writes across the main loop and worker threads.
    let _guard = write_lock.lock().unwrap();
    let resp = FanotifyResponse { fd: event_fd, response };
    let rc = unsafe {
        libc::write(
            fan_fd,
            &resp as *const _ as *const libc::c_void,
            mem::size_of::<FanotifyResponse>(),
        )
    };
    if rc < 0 {
        eprintln!("applockerd: failed to write response: {}", std::io::Error::last_os_error());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unescape_mountinfo_handles_octal() {
        assert_eq!(unescape_mountinfo("/tmp/applocker-test"), "/tmp/applocker-test");
        assert_eq!(unescape_mountinfo("/mnt/my\\040drive"), "/mnt/my drive"); // \040 = space
        assert_eq!(unescape_mountinfo("a\\011b"), "a\tb"); // \011 = tab
        assert_eq!(unescape_mountinfo("back\\134slash"), "back\\slash"); // \134 = backslash
        // A lone backslash not followed by 3 octal digits is left as-is.
        assert_eq!(unescape_mountinfo("trail\\"), "trail\\");
        assert_eq!(unescape_mountinfo("\\9ab"), "\\9ab");
    }

    #[test]
    fn is_mount_point_detects_real_mounts() {
        // /proc is always its own mount on Linux; a regular subdirectory is not.
        assert!(is_mount_point("/proc"));
        assert!(!is_mount_point("/proc/self")); // a dir within the proc mount
        assert!(!is_mount_point("/nonexistent-applocker-xyz"));
    }

    #[test]
    fn fail_open_timeout_parsing() {
        // Default (unset) is a 30s watchdog; "0" disables it. We can't safely
        // mutate process env in parallel tests, so just assert the default arm.
        assert_eq!(fail_open_timeout().map(|d| d.as_secs()), Some(30));
    }
}
