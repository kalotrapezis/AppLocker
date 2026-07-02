//! AppLocker daemon library — the DE-agnostic logic behind `applockerd`.
//!
//! The binary (`main.rs`) owns the fanotify event loop and process lifecycle;
//! everything reusable and unit-testable lives here so it can be exercised with
//! `cargo test` without root, a camera, or a display.

pub mod auth;
pub mod crypto;
pub mod desktop;
pub mod face;
pub mod feedback;
pub mod folderlist;
pub mod gate;
pub mod locklist;
pub mod pam;
pub mod pin;
pub mod policy;
pub mod session;
