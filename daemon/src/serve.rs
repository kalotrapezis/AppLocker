//! The **auth broker**: a persistent, root Unix-socket service that runs the
//! auth routine (face → PIN → sudo) for the GUI and then performs privileged
//! changes as root.
//!
//! Why this exists
//! ---------------
//! Settings used to make every privileged change with `pkexec applockerd …`.
//! `pkexec` is polkit, and **polkit only knows the sudo password** — it has no
//! idea the AppLocker PIN exists. So the PIN worked when *opening* Settings (that
//! goes through our own auth routine) but nowhere else, and a single "Apply"
//! could pop two polkit dialogs. Routing changes through this broker instead:
//!
//!   * the PIN works everywhere (the broker runs *our* routine, not polkit);
//!   * one "Apply" costs one prompt (a batch is one request, one auth);
//!   * no polkit at all — which also sidesteps the GTK-vs-KDE agent question.
//!
//! The broker is root but **never touches fanotify**, so unlike the gate it
//! cannot freeze input — it is safe to leave running. The dangerous enforcement
//! gate (`applockerd gate`) stays a separate, dev-gated unit that this broker can
//! start/stop on request.
//!
//! Wire protocol (line-based, request read to EOF)
//! -----------------------------------------------
//! The client writes a request and half-closes its write end; the broker reads to
//! EOF, acts, writes one reply line, and closes. Request lines:
//!
//! ```text
//!   line 0: verb        "authorize" | "apply"
//!   line 1: token       opaque per-window id (see below)
//!   line 2: reason       human text shown in the prompt title
//!   line 3: flag        "force" (always re-auth) | "-"
//!   line 4…: ops        only for "apply": each op = args joined by \t,
//!                        e.g. "lock-app\tFirefox" or "service\ton"
//! ```
//!
//! Reply is exactly one line: `ok`, `denied`, or `error <msg>`.
//!
//! Per-window auth caching
//! -----------------------
//! Opening Settings must require auth (tamper protection), but the user should
//! not be re-prompted for every little change inside that one window. So the
//! client mints a random *token* per process; the first `authorize` for a token
//! runs the routine and remembers the token (bounded TTL). Subsequent `apply`s
//! carrying the same token skip the prompt. A new window = new token = fresh
//! auth. `flag=force` ignores the token entirely — used for folder reveal, the
//! one place the user wants auth on *every* press.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::io::AsRawFd;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::process::Command;
use std::time::{Duration, Instant};

use crate::auth::{self, Available, Fallback, Method, Outcome, PromptResult, Prompter,
                  SystemFallback};
use crate::face;
use crate::feedback::{ClosingPrompter, FaceWithFeedback, Feedback};
use crate::gate::GuiPrompter;
use crate::pin;
use crate::session;
use crate::sudopass::{self, FailOutcome, LockStatus};

/// How long a window token stays authorized after its last successful auth.
/// Caps how long a closed-then-reopened Settings window can skip the prompt.
const TOKEN_TTL: Duration = Duration::from_secs(10 * 60);

/// The socket path: `$APPLOCKER_SOCK` (dev/tests) or the system default.
pub fn socket_path() -> PathBuf {
    match std::env::var_os("APPLOCKER_SOCK") {
        Some(p) => PathBuf::from(p),
        None => PathBuf::from("/run/applockerd.sock"),
    }
}

/// Entry point for `applockerd serve`. Never returns under normal operation.
pub fn run() -> ! {
    if unsafe { libc::geteuid() } != 0 {
        eprintln!("applockerd: serve (auth broker) must run as root (try: sudo applockerd serve)");
        std::process::exit(1);
    }
    // main() restores SIGPIPE to SIG_DFL for CLI piping; a long-lived server must
    // not die when a client hangs up mid-reply, so ignore it here. Writes then
    // fail with EPIPE, which we already discard.
    unsafe { libc::signal(libc::SIGPIPE, libc::SIG_IGN) };
    // CRITICAL: lock-app/unlock-app run `signal_daemon_reload()`, which SIGHUPs
    // EVERY `applockerd` process so the fanotify GATE reloads its list. The broker
    // is also an `applockerd` process but has no list to reload — and SIGHUP's
    // default action is *terminate*, so without this the broker was being killed
    // by the very child it spawned to do the change, then restarted by systemd.
    // The broker has nothing to reload, so it simply ignores SIGHUP.
    unsafe { libc::signal(libc::SIGHUP, libc::SIG_IGN) };

    let path = socket_path();
    // A stale socket file from a previous run would make bind() fail with
    // EADDRINUSE. It's ours to remove (we're the only thing that binds it).
    let _ = std::fs::remove_file(&path);
    let listener = match UnixListener::bind(&path) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("applockerd: cannot bind {}: {e}", path.display());
            std::process::exit(1);
        }
    };
    // The desktop user (a normal uid) must be able to connect; the per-request
    // SO_PEERCRED check is what actually restricts *who*. 0666 on a root-owned
    // socket in root-owned /run is connectable only by someone who can already
    // reach /run, and every real caller is then identity-checked below.
    if let Err(e) = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o666)) {
        eprintln!("applockerd: warning: cannot chmod {}: {e}", path.display());
    }

    eprintln!("applockerd: auth broker listening on {}", path.display());
    eprintln!("applockerd: (no fanotify here — this process can't freeze input)");

    // Authorized window tokens → when they were last proven. Single-threaded
    // accept loop (Settings is one client and prompts are modal anyway), so a
    // plain map with no lock is enough and prompts never stack.
    let mut tokens: HashMap<String, Instant> = HashMap::new();

    for conn in listener.incoming() {
        match conn {
            Ok(stream) => {
                // Isolate each request: a panic in one handler (bad input, a
                // subprocess quirk, a poisoned lock) must NOT take down the whole
                // broker and leave a dead socket behind. Catch it, log, carry on.
                let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(
                    || handle(stream, &mut tokens),
                ));
                if result.is_err() {
                    eprintln!("applockerd: broker handler panicked — request dropped, \
                               daemon still up.");
                }
            }
            Err(e) => eprintln!("applockerd: broker accept error: {e}"),
        }
    }
    // incoming() only ends if the listener dies; treat that as fatal.
    std::process::exit(1);
}

/// Serve one connection to completion.
fn handle(mut stream: UnixStream, tokens: &mut HashMap<String, Instant>) {
    // Reject anyone who isn't the active desktop user (or root, for CLI tests).
    match peer_uid(&stream) {
        Some(uid) if uid_allowed(uid) => {}
        Some(uid) => {
            eprintln!("applockerd: broker rejecting uid {uid} (not the desktop user)");
            let _ = stream.write_all(b"denied\n");
            return;
        }
        None => {
            eprintln!("applockerd: broker cannot read peer credentials — rejecting");
            let _ = stream.write_all(b"denied\n");
            return;
        }
    }

    let mut raw = String::new();
    if stream.read_to_string(&mut raw).is_err() {
        let _ = stream.write_all(b"error read\n");
        return;
    }
    let req = Request::parse(&raw);
    eprintln!("applockerd: broker request '{}' ({} op(s))", req.verb, req.ops.len());

    let reply = match req.verb.as_str() {
        "authorize" => {
            if gate_auth(tokens, &req.token, &req.reason, req.force) {
                "ok".to_string()
            } else {
                "denied".to_string()
            }
        }
        "apply" => {
            if !gate_auth(tokens, &req.token, &req.reason, req.force) {
                "denied".to_string()
            } else {
                match run_ops(&req.ops) {
                    Ok(()) => "ok".to_string(),
                    Err(e) => format!("error {e}"),
                }
            }
        }
        "verify" => {
            // No-UI PIN check, used by the polkit agent to let your PIN (not just
            // the sudo password) release the cached password. No prompt, no face,
            // no token — just "is this the right PIN?", gated by the PIN policy.
            if verify_pin(&req.ops) { "ok".to_string() } else { "denied".to_string() }
        }
        // ── sudo-password autocomplete (polkit agent) ─────────────────────────
        "store-sudo" => store_sudo(&req.ops),
        "get-sudo" => get_sudo(&req.reason),
        "forget-sudo" => {
            if !gate_auth(tokens, &req.token, &req.reason, req.force) {
                "denied".to_string()
            } else {
                sudopass::Store::system().forget();
                "ok".to_string()
            }
        }
        "set-pin" => {
            // Sensitive: always a fresh auth (like weakening a factor).
            if !gate_auth(tokens, &req.token, &req.reason, true) {
                "denied".to_string()
            } else {
                match set_pin(&req.ops) {
                    Ok(()) => "ok".to_string(),
                    Err(e) => format!("error {e}"),
                }
            }
        }
        other => format!("error unknown verb {other:?}"),
    };
    // Log only the first line: get-sudo's reply is `ok\n<password>`.
    eprintln!("applockerd: broker reply to '{}': {}",
              req.verb, reply.lines().next().unwrap_or(""));
    let _ = stream.write_all(reply.as_bytes());
    let _ = stream.write_all(b"\n");
}

/// A parsed request. See the module-level protocol description.
#[derive(Debug, PartialEq, Eq)]
struct Request {
    verb: String,
    token: String,
    reason: String,
    force: bool,
    ops: Vec<Vec<String>>,
}

impl Request {
    fn parse(raw: &str) -> Request {
        let mut lines = raw.lines();
        let verb = lines.next().unwrap_or("").trim().to_string();
        let token = lines.next().unwrap_or("").trim().to_string();
        let reason = {
            let r = lines.next().unwrap_or("").trim();
            if r.is_empty() { "AppLocker".to_string() } else { r.to_string() }
        };
        let force = lines.next().unwrap_or("-").trim() == "force";
        let ops = lines
            .filter(|l| !l.trim().is_empty())
            .map(|l| l.split('\t').map(|s| s.to_string()).collect())
            .collect();
        Request { verb, token, reason, force, ops }
    }
}

/// Decide whether this request may proceed, running the interactive auth routine
/// only when needed, and refreshing the token on success.
///
/// `force` always re-authenticates and never consults or updates the token
/// cache (folder reveal). Otherwise a live, non-expired token skips the prompt;
/// a fresh auth stamps the token so the rest of the window is covered.
fn gate_auth(tokens: &mut HashMap<String, Instant>, token: &str, reason: &str, force: bool) -> bool {
    if !force && token_live(tokens, token) {
        return true;
    }
    if !authenticate(reason) {
        return false;
    }
    if !force && !token.is_empty() {
        tokens.insert(token.to_string(), Instant::now());
    }
    true
}

/// Is `token` present and within its TTL? Prunes it if expired.
fn token_live(tokens: &mut HashMap<String, Instant>, token: &str) -> bool {
    if token.is_empty() {
        return false;
    }
    match tokens.get(token) {
        Some(t) if t.elapsed() < TOKEN_TTL => true,
        Some(_) => {
            tokens.remove(token);
            false
        }
        None => false,
    }
}

/// Run the full auth routine once (face → PIN/sudo prompt), returning true on
/// ALLOW. This mirrors `cmd_auth_test` in main.rs: the prompt and camera are
/// dropped into the user's graphical session by `GuiPrompter`/`session::attach`.
fn authenticate(reason: &str) -> bool {
    let (face_v, attempts, face_live) = face::build();
    let fb0 = SystemFallback::system();
    eprintln!(
        "applockerd: authenticate({reason:?}): face_live={face_live} attempts={attempts} \
         pin_available={} sudo_available={} — trying face first, then fallback prompt",
        fb0.pin_available(), fb0.sudo_available(),
    );
    let cfg = face::config_for(attempts);
    let fb = std::rc::Rc::new(std::cell::RefCell::new(if face_live {
        Feedback::spawn(reason)
    } else {
        Feedback::none()
    }));
    let mut face = FaceWithFeedback::new(face_v, fb.clone(), attempts);
    let mut prompter = ClosingPrompter::new(GuiPrompter::new(reason, face_live), fb);
    let fallback = SystemFallback::system();
    matches!(
        auth::run(&cfg, &mut face, &mut prompter, &fallback),
        Outcome::Allowed
    )
}

/// Execute each authorized op as a root child. `service on|off` maps to a
/// systemctl start/stop of the enforcement gate unit; everything else is an
/// `applockerd` subcommand re-invoked on our own binary (so all the existing
/// lock-app/set-fallback/… logic and its SIGHUP reload are reused untouched).
fn run_ops(ops: &[Vec<String>]) -> Result<(), String> {
    // The desktop user's uid, so `lock-app` resolves *their* app dirs. A plain
    // root child would read /root and never find user-installed apps (AppImages,
    // per-user flatpaks) — the exact reason add-app was silently failing.
    // `desktop::user_home()` honours PKEXEC_UID first, so we mirror pkexec here.
    let desktop_uid = session::SessionCtx::discover().map(|c| c.uid);
    for op in ops {
        let Some(head) = op.first() else { continue };
        let status = if head == "service" {
            let action = match op.get(1).map(String::as_str) {
                Some("on") => "start",
                Some("off") => "stop",
                other => return Err(format!("bad service action {other:?}")),
            };
            Command::new("systemctl")
                .arg(action)
                .arg("applockerd.service")
                .status()
        } else if head == "pam" {
            // Enable/disable a face-unlock PAM tier via applocker-pam (always
            // `auth sufficient`, so the password still works — can't lock out).
            // Root-only; that's why it rides the broker. Whitelist the tier so a
            // client can't pass an arbitrary applocker-pam argument.
            let tier = match op.get(1).map(String::as_str) {
                Some(t @ ("sudo" | "uisudo" | "screenlock")) => t,
                other => return Err(format!("bad pam tier {other:?}")),
            };
            let action = match op.get(2).map(String::as_str) {
                Some("on") => "enable",
                Some("off") => "disable",
                other => return Err(format!("bad pam action {other:?}")),
            };
            Command::new("/usr/bin/applocker-pam")
                .arg(action)
                .arg(tier)
                .status()
        } else {
            let exe = std::env::current_exe()
                .map_err(|e| format!("current_exe: {e}"))?;
            let mut cmd = Command::new(exe);
            cmd.args(op);
            if let Some(uid) = desktop_uid {
                cmd.env("PKEXEC_UID", uid.to_string());
            }
            cmd.status()
        };
        match status {
            Ok(s) if s.success() => {}
            Ok(s) => return Err(format!("{} exited {}", op.join(" "), s.code().unwrap_or(-1))),
            Err(e) => return Err(format!("{}: {e}", op.join(" "))),
        }
    }
    Ok(())
}

/// Verify a PIN carried as a `pin\t<secret>` op, respecting the PIN policy
/// (`allow_pin` + a PIN actually being set). No face, no prompt — the caller
/// (polkit agent) has its own reason to trust the outcome.
fn verify_pin(ops: &[Vec<String>]) -> bool {
    let fb = SystemFallback::system();
    if !fb.pin_available() {
        return false;
    }
    ops.iter().any(|op| match op.as_slice() {
        [kind, secret] if kind == "pin" => fb.verify_pin(secret),
        _ => false,
    })
}

/// Extract the secret of a `<kind>\t<secret>` op (e.g. `pw` or `pin`).
fn op_secret<'a>(ops: &'a [Vec<String>], kind: &str) -> Option<&'a str> {
    ops.iter().find_map(|op| match op.as_slice() {
        [k, secret] if k == kind => Some(secret.as_str()),
        _ => None,
    })
}

/// Store the sudo password for autocomplete — but only after PAM confirms it's
/// the real one, so we never persist a wrong password.
fn store_sudo(ops: &[Vec<String>]) -> String {
    let Some(pw) = op_secret(ops, "pw") else {
        return "error missing pw".to_string();
    };
    let fb = SystemFallback::system();
    match fb.verify_password(pw) {
        Ok(true) => match sudopass::Store::system().store(pw) {
            Ok(()) => "ok".to_string(),
            Err(e) => format!("error {e}"),
        },
        Ok(false) => "denied".to_string(),
        Err(e) => format!("error {e}"),
    }
}

/// Release the stored sudo password to the polkit agent, gated by face → PIN
/// (with the 2→24h→2→destroy lockout on wrong PINs). The sudo-password option is
/// always offered too, so a forgotten PIN or an active lock never blocks you.
///
/// Replies: `ok\n<password>`, `denied`, `locked <secs>`, `destroyed`, `cancel`,
/// or `notset`.
fn get_sudo(reason: &str) -> String {
    let store = sudopass::Store::system();
    if !store.is_set() {
        return "notset".to_string();
    }
    let now = sudopass::now_secs();
    let locked = matches!(store.status(now), LockStatus::Locked { .. });

    // Frictionless path: a face match releases it with no typing.
    let (mut face_v, attempts, face_live) = face::build();
    for _ in 0..attempts {
        if face_v.try_match() {
            store.record_success();
            return match store.load() {
                Some(pw) => format!("ok\n{pw}"),
                None => "notset".to_string(),
            };
        }
    }

    let fb = SystemFallback::system();
    // Offer the PIN only when it's usable and not currently locked; the sudo
    // password is always offered as the escape hatch.
    let available = Available { pin: fb.pin_available() && !locked, sudo: true };
    let mut prompter = GuiPrompter::new(reason, face_live);
    match prompter.prompt(available) {
        PromptResult::Cancelled => "cancel".to_string(),
        PromptResult::Entered { method: Method::Pin, secret } => {
            if fb.verify_pin(&secret) {
                store.record_success();
                match store.load() {
                    Some(pw) => format!("ok\n{pw}"),
                    None => "notset".to_string(),
                }
            } else {
                match store.record_failure(now) {
                    FailOutcome::Denied => "denied".to_string(),
                    FailOutcome::Locked { secs } => format!("locked {secs}"),
                    FailOutcome::Destroyed => "destroyed".to_string(),
                }
            }
        }
        PromptResult::Entered { method: Method::Password, secret } => {
            if fb.verify_password(&secret).unwrap_or(false) {
                // Correct real password: clears the lockout and refreshes the
                // stored copy (handles a changed password).
                store.record_success();
                let _ = store.store(&secret);
                format!("ok\n{secret}")
            } else {
                "denied".to_string()
            }
        }
    }
}

/// Change the PIN (already behind a fresh auth in the caller). Also enables the
/// PIN in policy and clears any lockout so a new PIN starts clean.
fn set_pin(ops: &[Vec<String>]) -> Result<(), String> {
    let Some(new) = op_secret(ops, "pin") else {
        return Err("missing pin".to_string());
    };
    pin::set_pin(&auth::default_pin_path(), new).map_err(|e| e.to_string())?;
    let mut pol = crate::policy::load_default();
    pol.allow_pin = true;
    let _ = pol.save(&crate::policy::default_path());
    sudopass::Store::system().record_success(); // wipe stale lockout state
    Ok(())
}

/// The connecting process' uid via `SO_PEERCRED`. `None` if it can't be read.
fn peer_uid(stream: &UnixStream) -> Option<u32> {
    let fd = stream.as_raw_fd();
    let mut cred = libc::ucred { pid: 0, uid: 0, gid: 0 };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    let rc = unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut cred as *mut libc::ucred as *mut libc::c_void,
            &mut len,
        )
    };
    (rc == 0).then_some(cred.uid)
}

/// Only the active desktop user may drive the broker (root is allowed too, for
/// `sudo applockerd serve` + CLI testing). This stops another logged-in local
/// user from asking the root broker to unlock things.
fn uid_allowed(uid: u32) -> bool {
    if uid == 0 {
        return true;
    }
    match session::SessionCtx::discover() {
        Some(ctx) => ctx.uid == uid,
        // No graphical session resolvable: fail closed for non-root.
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_authorize() {
        let r = Request::parse("authorize\nabc123\nAppLocker settings\n-\n");
        assert_eq!(r.verb, "authorize");
        assert_eq!(r.token, "abc123");
        assert_eq!(r.reason, "AppLocker settings");
        assert!(!r.force);
        assert!(r.ops.is_empty());
    }

    #[test]
    fn parse_apply_batch_with_tabs() {
        let raw = "apply\ntok\nApply settings\n-\nset-fallback\tboth\nlock-app\tFirefox\n";
        let r = Request::parse(raw);
        assert_eq!(r.verb, "apply");
        assert!(!r.force);
        assert_eq!(
            r.ops,
            vec![
                vec!["set-fallback".to_string(), "both".to_string()],
                vec!["lock-app".to_string(), "Firefox".to_string()],
            ]
        );
    }

    #[test]
    fn parse_verify_pin_op() {
        let r = Request::parse("verify\n\npolkit\n-\npin\t1234\n");
        assert_eq!(r.verb, "verify");
        assert_eq!(r.ops, vec![vec!["pin".to_string(), "1234".to_string()]]);
    }

    #[test]
    fn parse_force_flag_and_service_op() {
        let r = Request::parse("apply\ntok\nStart AppLocker gate\nforce\nservice\ton\n");
        assert!(r.force);
        assert_eq!(r.ops, vec![vec!["service".to_string(), "on".to_string()]]);
    }

    #[test]
    fn parse_empty_reason_defaults() {
        // Blank reason line must not produce an empty prompt title.
        let r = Request::parse("authorize\ntok\n\n-\n");
        assert_eq!(r.reason, "AppLocker");
    }

    #[test]
    fn parse_blank_op_lines_ignored() {
        let r = Request::parse("apply\ntok\nr\n-\n\nlock-app\tX\n\n");
        assert_eq!(r.ops, vec![vec!["lock-app".to_string(), "X".to_string()]]);
    }

    #[test]
    fn token_ttl_expiry() {
        let mut tokens: HashMap<String, Instant> = HashMap::new();
        assert!(!token_live(&mut tokens, "t"));
        tokens.insert("t".to_string(), Instant::now());
        assert!(token_live(&mut tokens, "t"));
        // A clearly-expired stamp is pruned and reported dead.
        tokens.insert("old".to_string(), Instant::now() - TOKEN_TTL - Duration::from_secs(1));
        assert!(!token_live(&mut tokens, "old"));
        assert!(!tokens.contains_key("old"));
        // Empty token is never live.
        assert!(!token_live(&mut tokens, ""));
    }
}
