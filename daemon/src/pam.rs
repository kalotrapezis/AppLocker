//! Sudo-password fallback via PAM, loaded at runtime with `dlopen`.
//!
//! Why dlopen and not `-lpam`: the target boxes ship `libpam.so.0` (the runtime
//! library) but not necessarily the `libpam.so` dev symlink, so link-time
//! `-lpam` would fail on a bare Mint install. Resolving the handful of symbols
//! we need at runtime removes the build-time dependency entirely and still fails
//! loud if PAM is somehow absent.
//!
//! **This must run in the root daemon, never in the GUI.** The GUI only collects
//! the secret and hands it back; the actual accept/reject decision is made here,
//! so a tampered prompt cannot fake a success (see the "settings tamper" note in
//! ../README.md).
//!
//! We authenticate the given user against a PAM service (default: `sudo`, which
//! every sudo-capable Mint box has) and also run account management, so a locked
//! or expired account is rejected rather than waved through.

use std::ffi::{c_char, c_int, c_void, CStr, CString};
use std::ptr;

// ── PAM ABI constants (stable across Linux-PAM) ──────────────────────────────
const PAM_SUCCESS: c_int = 0;
const PAM_PROMPT_ECHO_OFF: c_int = 1; // password-style prompt
const PAM_PROMPT_ECHO_ON: c_int = 2; // visible prompt (e.g. a login name)
const PAM_MAX_RESP_SIZE: usize = 512;

#[repr(C)]
struct PamMessage {
    msg_style: c_int,
    msg: *const c_char,
}

#[repr(C)]
struct PamResponse {
    resp: *mut c_char,
    resp_retcode: c_int,
}

#[repr(C)]
struct PamConv {
    conv: Option<
        extern "C" fn(
            num_msg: c_int,
            msg: *mut *const PamMessage,
            resp: *mut *mut PamResponse,
            appdata_ptr: *mut c_void,
        ) -> c_int,
    >,
    appdata_ptr: *mut c_void,
}

// Function-pointer types for the four symbols we resolve from libpam.
type PamStartFn = unsafe extern "C" fn(
    service: *const c_char,
    user: *const c_char,
    conv: *const PamConv,
    handle: *mut *mut c_void,
) -> c_int;
type PamAuthenticateFn = unsafe extern "C" fn(handle: *mut c_void, flags: c_int) -> c_int;
type PamAcctMgmtFn = unsafe extern "C" fn(handle: *mut c_void, flags: c_int) -> c_int;
type PamEndFn = unsafe extern "C" fn(handle: *mut c_void, status: c_int) -> c_int;

/// The password to feed the conversation, passed via `appdata_ptr`.
struct ConvData {
    password: CString,
}

/// PAM conversation callback: answer every echo-off/echo-on prompt with the one
/// password we were given. `malloc` the response array/strings because PAM frees
/// them with `free()`.
extern "C" fn conversation(
    num_msg: c_int,
    _msg: *mut *const PamMessage,
    resp: *mut *mut PamResponse,
    appdata_ptr: *mut c_void,
) -> c_int {
    if num_msg <= 0 || resp.is_null() || appdata_ptr.is_null() {
        return 19; // PAM_CONV_ERR
    }
    let data = unsafe { &*(appdata_ptr as *const ConvData) };
    let n = num_msg as usize;

    unsafe {
        let array =
            libc::calloc(n, std::mem::size_of::<PamResponse>()) as *mut PamResponse;
        if array.is_null() {
            return 5; // PAM_BUF_ERR
        }

        for i in 0..n {
            let style = (**_msg.add(i)).msg_style;
            let entry = array.add(i);
            if style == PAM_PROMPT_ECHO_OFF || style == PAM_PROMPT_ECHO_ON {
                let bytes = data.password.as_bytes_with_nul();
                if bytes.len() > PAM_MAX_RESP_SIZE {
                    libc::free(array as *mut c_void);
                    return 5;
                }
                let buf = libc::malloc(bytes.len()) as *mut c_char;
                if buf.is_null() {
                    libc::free(array as *mut c_void);
                    return 5;
                }
                ptr::copy_nonoverlapping(bytes.as_ptr() as *const c_char, buf, bytes.len());
                (*entry).resp = buf;
            } else {
                (*entry).resp = ptr::null_mut();
            }
            (*entry).resp_retcode = 0;
        }
        *resp = array;
    }
    PAM_SUCCESS
}

/// Resolved libpam entry points.
struct Pam {
    handle: *mut c_void, // dlopen handle
    start: PamStartFn,
    authenticate: PamAuthenticateFn,
    acct_mgmt: PamAcctMgmtFn,
    end: PamEndFn,
}

impl Pam {
    fn load() -> Result<Pam, String> {
        unsafe {
            let name = CString::new("libpam.so.0").unwrap();
            let handle = libc::dlopen(name.as_ptr(), libc::RTLD_NOW);
            if handle.is_null() {
                return Err("could not dlopen libpam.so.0".to_string());
            }
            let sym = |s: &str| -> Result<*mut c_void, String> {
                let c = CString::new(s).unwrap();
                let p = libc::dlsym(handle, c.as_ptr());
                if p.is_null() {
                    Err(format!("libpam missing symbol {s}"))
                } else {
                    Ok(p)
                }
            };
            Ok(Pam {
                start: std::mem::transmute::<*mut c_void, PamStartFn>(sym("pam_start")?),
                authenticate: std::mem::transmute::<*mut c_void, PamAuthenticateFn>(sym(
                    "pam_authenticate",
                )?),
                acct_mgmt: std::mem::transmute::<*mut c_void, PamAcctMgmtFn>(sym("pam_acct_mgmt")?),
                end: std::mem::transmute::<*mut c_void, PamEndFn>(sym("pam_end")?),
                handle,
            })
        }
    }
}

impl Drop for Pam {
    fn drop(&mut self) {
        unsafe {
            libc::dlclose(self.handle);
        }
    }
}

/// Verify `password` for `user` against PAM `service` (use `"sudo"` for the
/// sudo-password fallback). Returns `Ok(true)` on accept, `Ok(false)` on a wrong
/// password or a failed account check, and `Err` only if PAM itself is
/// unavailable — the caller treats that as "fallback broken", never as success.
pub fn verify_password(service: &str, user: &str, password: &str) -> Result<bool, String> {
    let pam = Pam::load()?;

    let data = ConvData {
        password: CString::new(password).map_err(|_| "password contains NUL".to_string())?,
    };
    let conv = PamConv {
        conv: Some(conversation),
        appdata_ptr: &data as *const ConvData as *mut c_void,
    };

    let c_service = CString::new(service).map_err(|_| "bad service name".to_string())?;
    let c_user = CString::new(user).map_err(|_| "bad user name".to_string())?;
    let mut handle: *mut c_void = ptr::null_mut();

    unsafe {
        let rc = (pam.start)(c_service.as_ptr(), c_user.as_ptr(), &conv, &mut handle);
        if rc != PAM_SUCCESS {
            return Err(format!("pam_start failed (rc={rc})"));
        }
        // 0 flags: don't set PAM_DISALLOW_NULL_AUTHTOK — the service policy decides.
        let auth_rc = (pam.authenticate)(handle, 0);
        let acct_rc = if auth_rc == PAM_SUCCESS {
            (pam.acct_mgmt)(handle, 0)
        } else {
            auth_rc
        };
        (pam.end)(handle, auth_rc);

        Ok(auth_rc == PAM_SUCCESS && acct_rc == PAM_SUCCESS)
    }
}

/// The login name PAM should authenticate against: whoever owns the daemon's
/// controlling session in the real deployment. For the spike we take the user
/// that launched us via `SUDO_USER` (set by sudo), falling back to the effective
/// user name. Returns `None` if neither can be resolved.
pub fn invoking_user() -> Option<String> {
    if let Ok(u) = std::env::var("SUDO_USER") {
        if !u.is_empty() {
            return Some(u);
        }
    }
    // Fall back to the effective uid's passwd name.
    unsafe {
        let uid = libc::geteuid();
        let pw = libc::getpwuid(uid);
        if pw.is_null() {
            return None;
        }
        let name = CStr::from_ptr((*pw).pw_name);
        name.to_str().ok().map(|s| s.to_string())
    }
}
