//! PIN fallback: a salted PBKDF2 hash on disk, set once and checked at auth time.
//!
//! File format (one line, `$`-separated), deliberately self-describing so the
//! iteration count can be raised later without breaking old files:
//!
//! ```text
//! pbkdf2_sha256$<rounds>$<salt_hex>$<hash_hex>
//! ```
//!
//! The PIN is the *weaker* of the two always-available fallbacks (the other is
//! the sudo password via `pam.rs`); see the threat model in ../README.md. We
//! still salt + stretch it so the on-disk file isn't a plain digest.

use std::fs;
use std::io::{self, Read, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::Path;

use crate::crypto;

/// Iteration count for new PINs. High enough to make guessing a 4–8 digit PIN
/// from a stolen hash slow, low enough to keep an interactive check instant.
const DEFAULT_ROUNDS: u32 = 200_000;
const SALT_LEN: usize = 16;

/// Read 16 random bytes for a fresh salt. `/dev/urandom` never short-reads in
/// practice for a request this small, but we loop to be correct.
fn random_salt() -> io::Result<[u8; SALT_LEN]> {
    let mut f = fs::File::open("/dev/urandom")?;
    let mut salt = [0u8; SALT_LEN];
    f.read_exact(&mut salt)?;
    Ok(salt)
}

/// Hash `pin` and write the record to `path` with `0600` perms. Overwrites any
/// existing PIN.
pub fn set_pin(path: &Path, pin: &str) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let salt = random_salt()?;
    let hash = crypto::pbkdf2_sha256(pin.as_bytes(), &salt, DEFAULT_ROUNDS);
    let line = format!(
        "pbkdf2_sha256${}${}${}\n",
        DEFAULT_ROUNDS,
        crypto::to_hex(&salt),
        crypto::to_hex(&hash)
    );

    // Write 0600 from the start so the hash is never briefly world-readable.
    let mut f = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)?;
    f.write_all(line.as_bytes())?;
    // create() honours `mode` only on creation; force it in case the file existed.
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    Ok(())
}

/// True if a PIN record exists at `path`.
pub fn is_set(path: &Path) -> bool {
    path.exists()
}

/// Verify `pin` against the record at `path`. Returns `Ok(false)` for a wrong
/// PIN, and an error only if the file is missing or malformed.
pub fn verify_pin(path: &Path, pin: &str) -> io::Result<bool> {
    let raw = fs::read_to_string(path)?;
    let record = raw.trim();
    let parts: Vec<&str> = record.split('$').collect();
    if parts.len() != 4 || parts[0] != "pbkdf2_sha256" {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "malformed PIN record",
        ));
    }
    let rounds: u32 = parts[1]
        .parse()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "bad rounds"))?;
    let salt = crypto::from_hex(parts[2])
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "bad salt hex"))?;
    let expected = crypto::from_hex(parts[3])
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "bad hash hex"))?;

    let got = crypto::pbkdf2_sha256(pin.as_bytes(), &salt, rounds);
    Ok(crypto::ct_eq(&got, &expected))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_path(name: &str) -> std::path::PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("applocker_pin_test_{}_{}", std::process::id(), name));
        p
    }

    #[test]
    fn set_then_verify_roundtrip() {
        let path = tmp_path("roundtrip");
        let _ = fs::remove_file(&path);
        set_pin(&path, "1379").unwrap();

        assert!(is_set(&path));
        assert!(verify_pin(&path, "1379").unwrap());
        assert!(!verify_pin(&path, "0000").unwrap());
        assert!(!verify_pin(&path, "13790").unwrap());

        // File must be 0600.
        let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "PIN file must be private");

        fs::remove_file(&path).unwrap();
    }

    #[test]
    fn resetting_pin_changes_salt() {
        let path = tmp_path("reset");
        let _ = fs::remove_file(&path);
        set_pin(&path, "1234").unwrap();
        let first = fs::read_to_string(&path).unwrap();
        set_pin(&path, "1234").unwrap();
        let second = fs::read_to_string(&path).unwrap();
        assert_ne!(first, second, "same PIN must produce a new salt each set");
        assert!(verify_pin(&path, "1234").unwrap());
        fs::remove_file(&path).unwrap();
    }

    #[test]
    fn malformed_record_errors() {
        let path = tmp_path("malformed");
        fs::write(&path, "not-a-valid-record\n").unwrap();
        assert!(verify_pin(&path, "1234").is_err());
        fs::remove_file(&path).unwrap();
    }
}
