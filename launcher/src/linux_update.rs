//! Linux first-release updates download and verify only. Never mutate an installed bundle.
use crate::{
    standard_update::verify_sha256_file,
    update_manifest::{is_sha256_hex, UpdateCheck},
};
use anyhow::{anyhow, bail, Context, Result};
use std::{
    fs::{self, File, OpenOptions},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{SystemTime, UNIX_EPOCH},
};

pub(crate) fn download_verified(check: &UpdateCheck, data_dir: &Path) -> Result<PathBuf> {
    let url = check
        .download_url
        .as_deref()
        .ok_or_else(|| anyhow!("No matching Linux update package is published"))?;
    if !url.starts_with("https://") || url.chars().any(char::is_control) {
        bail!("Update package must use HTTPS");
    }
    let extension = match check.package.as_deref() {
        Some("portable-tar-gz") => "tar.gz",
        Some("standard-deb") => "deb",
        _ => bail!("Unsupported Linux update package type"),
    };
    let hash = check
        .download_sha256
        .as_deref()
        .filter(|v| is_sha256_hex(v))
        .ok_or_else(|| anyhow!("Missing or invalid update SHA256"))?;
    let directory = data_dir.join("downloads");
    fs::create_dir_all(&directory)?;
    // Names derive only from validated hash/package, never remote paths or version strings.
    let destination = directory.join(format!("ilab-conjure-{}.{}", &hash[..16], extension));
    download_with(&destination, hash, |file| {
        let output = Command::new("curl")
            .args([
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--connect-timeout",
                "15",
                "--max-time",
                "600",
                "--",
                url,
            ])
            .stdout(Stdio::from(file))
            .stderr(Stdio::piped())
            .output()
            .context("Could not run curl to download the update")?;
        if !output.status.success() {
            bail!(
                "Update download failed: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            );
        }
        Ok(())
    })?;
    Ok(destination)
}

fn download_with(
    destination: &Path,
    hash: &str,
    transfer: impl FnOnce(File) -> Result<()>,
) -> Result<()> {
    let temp = destination.with_extension(format!(
        "{}-{}.part",
        std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos()
    ));
    let result = (|| {
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp)?;
        // FlushFileBuffers on Windows requires a writable handle. Retain a
        // clone of the original writer instead of reopening the file read-only.
        let durability_handle = file.try_clone()?;
        transfer(file)?;
        verify_sha256_file(&temp, hash)?;
        durability_handle.sync_all()?;
        drop(durability_handle); // Close every local file handle before promotion.
        fs::rename(&temp, destination).context("Could not save the verified update")?;
        Ok(())
    })();
    if temp.exists() {
        let _ = fs::remove_file(&temp);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    fn directory(test: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!("ilab-download-{test}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&p);
        fs::create_dir_all(&p).unwrap();
        p
    }
    #[test]
    fn streamed_download_is_saved_only_after_hash_verification() {
        let dir = directory("verified");
        let target = dir.join("package.deb");
        let hash = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
        download_with(&target, hash, |mut file| {
            file.write_all(b"abc")?;
            Ok(())
        })
        .unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"abc");
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 1);
        fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn verified_download_replaces_existing_file_without_partial_files() {
        let dir = directory("replace-verified");
        let target = dir.join("package.deb");
        fs::write(&target, b"previous verified version").unwrap();
        let hash = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
        download_with(&target, hash, |mut file| {
            file.write_all(b"abc")?;
            Ok(())
        })
        .unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"abc");
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 1);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn corrupt_or_failed_download_leaves_existing_package_unchanged() {
        let dir = directory("tampered");
        let target = dir.join("package.tar.gz");
        fs::write(&target, b"previous").unwrap();
        assert!(download_with(&target, &"a".repeat(64), |mut file| {
            file.write_all(b"tampered")?;
            Ok(())
        })
        .is_err());
        assert!(download_with(&target, &"a".repeat(64), |mut file| {
            file.write_all(b"partial")?;
            bail!("interrupted")
        })
        .is_err());
        assert_eq!(fs::read(&target).unwrap(), b"previous");
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 1);
        fs::remove_dir_all(dir).unwrap();
    }
}
