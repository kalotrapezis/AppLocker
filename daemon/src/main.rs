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
//! Still NOT here (later steps): the real face pipeline, the file-gate,
//! multi-mount marks, logind lock integration + cache wipe, D-Bus, a fail-open
//! watchdog. This is step 2, not the finished product.

use std::ffi::CString;
use std::fs;
use std::io::{self, Write};
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

// ── fanotify constants ──────────────────────────────────────────────────────
// Defined locally rather than relying on libc exposing every one of them, so
// the spike builds against an older libc as long as the *kernel* is new enough.

const FAN_CLOEXEC: libc::c_uint = 0x0000_0001;
const FAN_CLASS_CONTENT: libc::c_uint = 0x0000_0004; // required for PERM events

const FAN_MARK_ADD: libc::c_uint = 0x0000_0001;
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
    let mut prompter = ClosingPrompter::new(GuiPrompter::new(&app), fb);
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
    let folders = FolderList::load(&folderlist::default_path());
    let n_apps = apps.apps.len();
    let n_folders = folders.folders.len();
    let gate_files = !folders.is_empty();
    if n_apps == 0 && n_folders == 0 {
        eprintln!("applockerd: nothing locked yet — add with lock-app / lock-folder.");
    }
    let locks = Arc::new(RwLock::new(Locks { apps, folders }));

    let fan_fd = init_fanotify().unwrap_or_else(|e| {
        eprintln!("applockerd: fanotify_init failed: {e}");
        process::exit(1);
    });

    // Always gate execs (app-gate). Add file opens (file-gate) only when folders
    // are locked — FAN_OPEN_PERM on the whole fs intercepts *every* open, so we
    // don't pay that cost unless the user is actually locking folders.
    let mut mask = FAN_OPEN_EXEC_PERM;
    if gate_files {
        mask |= FAN_OPEN_PERM;
    }
    if let Err(e) = mark_filesystem(fan_fd, "/", mask) {
        eprintln!("applockerd: fanotify_mark failed: {e}");
        process::exit(1);
    }

    // Reload both lists live on SIGHUP (lock-*/unlock-* send it).
    unsafe { libc::signal(libc::SIGHUP, on_sighup as *const () as libc::sighandler_t) };

    eprintln!("applockerd: gating {n_apps} app(s) + {n_folders} folder(s) on /.");
    if gate_files {
        eprintln!("applockerd: file-gate ON — every file open is checked (may add latency).");
    }
    eprintln!("applockerd: policy: {}", policy::load_default().summary());
    eprintln!("applockerd: (PIN set: {})", pin::is_set(&auth::default_pin_path()));
    eprintln!("applockerd: Ctrl-C to stop; SIGHUP reloads the lists.");

    // Shared across worker threads: the unlock cache and a lock serialising the
    // fixed-size response writes to the fanotify fd.
    let cache = Arc::new(UnlockCache::new());
    let write_lock = Arc::new(Mutex::new(()));

    event_loop(fan_fd, locks, cache, write_lock);
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

fn mark_filesystem(fan_fd: RawFd, path: &str, mask: u64) -> std::io::Result<()> {
    // FAN_MARK_FILESYSTEM covers the whole filesystem containing `path`, so any
    // exec (and, if requested, any open) on that fs generates an event. (A
    // separate /home or flatpak store is a different fs and would need its own
    // mark — noted in README as a known gap.)
    let c_path = CString::new(path).unwrap();
    let rc = unsafe {
        libc::fanotify_mark(
            fan_fd,
            FAN_MARK_ADD | FAN_MARK_FILESYSTEM,
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
) {
    let mut buf = [0u8; 8192];
    let meta_size = mem::size_of::<libc::fanotify_event_metadata>();

    loop {
        // Apply a pending SIGHUP reload before blocking again.
        if RELOAD_LOCKS.swap(false, Ordering::SeqCst) {
            let apps = LockList::load(&locklist::default_path());
            let folders = FolderList::load(&folderlist::default_path());
            let (na, nf) = (apps.apps.len(), folders.folders.len());
            // If folders are now locked, make sure the file-gate mark is present
            // (FAN_MARK_ADD is idempotent). This lets the first locked folder take
            // effect live, without a restart. Fully *disabling* the file-gate
            // still needs a restart — we leave the mark rather than churn it.
            if !folders.is_empty() {
                let _ = mark_filesystem(fan_fd, "/", FAN_OPEN_EXEC_PERM | FAN_OPEN_PERM);
            }
            *locks.write().unwrap() = Locks { apps, folders };
            eprintln!("applockerd: reloaded lists ({na} app(s), {nf} folder(s)).");
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
                let (face, attempts, face_live) = face::build();
                let cfg = face::config_for(attempts);
                let fb = std::rc::Rc::new(std::cell::RefCell::new(if face_live {
                    Feedback::spawn(&app)
                } else {
                    Feedback::none()
                }));
                let mut face = FaceWithFeedback::new(face, fb.clone(), attempts);
                let mut prompter = ClosingPrompter::new(GuiPrompter::new(&app), fb);
                let fallback = SystemFallback::system();
                let outcome = auth::run(&cfg, &mut face, &mut prompter, &fallback);

                let allowed = matches!(outcome, Outcome::Allowed);
                cache.finish(&target, allowed, cache_policy);
                respond(
                    fan_fd,
                    event_fd,
                    if allowed { FAN_ALLOW } else { FAN_DENY },
                    &write_lock,
                );
                unsafe { libc::close(event_fd) };
                println!(
                    "{:<6} pid={pid:<7} {path} (auth {})",
                    if allowed { "allow" } else { "DENY" },
                    if allowed { "passed" } else { "failed" }
                );
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
