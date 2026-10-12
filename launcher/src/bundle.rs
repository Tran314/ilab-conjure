//! Package identity is explicit on Linux. A damaged package never becomes a source checkout.
use anyhow::{anyhow, Context, Result};
use std::{
    env, fs,
    path::{Component, Path, PathBuf},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BundleMode {
    Portable,
    Installed,
    Source,
}
impl BundleMode {
    pub fn launcher_mode(self) -> &'static str {
        match self {
            Self::Portable => "portable",
            Self::Installed => "standard",
            Self::Source => "source",
        }
    }
}

pub fn mode_for_app(app: &Path) -> Result<BundleMode> {
    let root = app.parent().unwrap_or(app);
    let metadata = root.join("bundle-metadata.json");
    if metadata.exists() {
        let value: serde_json::Value = serde_json::from_slice(&fs::read(&metadata)?)
            .context("Package damaged: invalid bundle-metadata.json; reinstall the package")?;
        let mode = match value.get("mode").and_then(|v| v.as_str()) {
            Some("portable") => BundleMode::Portable,
            Some("installed") => BundleMode::Installed,
            _ => {
                return Err(anyhow!(
                    "Package damaged: invalid bundle mode; reinstall the package"
                ))
            }
        };
        let package = match mode {
            BundleMode::Portable => "portable-tar-gz",
            _ => "standard-deb",
        };
        if value.get("schema_version").and_then(|v| v.as_u64()) != Some(1)
            || value.get("platform").and_then(|v| v.as_str()) != Some("linux-x86_64")
            || value
                .get("package")
                .and_then(|v| v.as_str())
                .is_some_and(|v| v != package)
        {
            return Err(anyhow!(
                "Package damaged: unsupported bundle metadata; reinstall the package"
            ));
        }
        return Ok(mode);
    }
    if cfg!(target_os = "linux")
        && (app.join("linux_webui_app.py").exists()
            || root.join("ilab-conjure-launcher").exists()
            || root.join("python").exists())
    {
        return Err(anyhow!(
            "Package damaged: bundle-metadata.json is missing; reinstall the package"
        ));
    }
    if cfg!(target_os = "linux") {
        return Ok(BundleMode::Source);
    }
    // Legacy macOS and Windows bundles remain supported. Do not permit a package
    // named app to bootstrap a system Python after its embedded runtime is lost.
    if super::is_legacy_standard_app_dir(app) {
        Ok(BundleMode::Installed)
    } else if app.file_name().and_then(|v| v.to_str()) == Some("app") {
        Ok(BundleMode::Portable)
    } else {
        Ok(BundleMode::Source)
    }
}

pub fn absolute_path(path: &Path) -> Result<PathBuf> {
    let path = if path.is_absolute() {
        path.to_path_buf()
    } else {
        env::current_dir()?.join(path)
    };
    if let Ok(real) = path.canonicalize() {
        return Ok(real);
    }
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            value => normalized.push(value.as_os_str()),
        }
    }
    // Resolve existing symlinked parents even when the final directory is new.
    if let Some(parent) = normalized.parent() {
        if parent != normalized {
            if let Some(name) = normalized.file_name() {
                return Ok(absolute_path(parent)?.join(name));
            }
        }
    }
    Ok(normalized)
}

pub fn linux_data_dir(xdg: Option<&Path>, home: Option<&Path>) -> Result<PathBuf> {
    let root = xdg
        .filter(|p| p.is_absolute())
        .map(Path::to_path_buf)
        .or_else(|| {
            home.filter(|p| p.is_absolute())
                .map(|p| p.join(".local/share"))
        })
        .ok_or_else(|| {
            anyhow!("Cannot resolve data directory: set an absolute HOME or XDG_DATA_HOME")
        })?;
    Ok(root.join(super::LEGACY_DATA_DIR_NAME))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn xdg_requires_absolute_path_and_preserves_legacy_storage_name() {
        let home = Path::new("/home/test");
        assert_eq!(
            linux_data_dir(Some(Path::new("/custom/data")), Some(home)).unwrap(),
            PathBuf::from("/custom/data/iLab GPT CONJURE")
        );
        for invalid in ["", "relative/path", "~/data"] {
            assert_eq!(
                linux_data_dir(Some(Path::new(invalid)), Some(home)).unwrap(),
                PathBuf::from("/home/test/.local/share/iLab GPT CONJURE")
            );
        }
        assert!(linux_data_dir(None, Some(Path::new("relative"))).is_err());
    }
    #[test]
    fn new_paths_resolve_existing_symlinked_parents() {
        let root = env::temp_dir().join(format!("ilab-absolute-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("real")).unwrap();
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(root.join("real"), root.join("link")).unwrap();
            assert_eq!(
                absolute_path(&root.join("link/new/data")).unwrap(),
                root.join("real/new/data")
            );
        }
        fs::remove_dir_all(root).unwrap();
    }
}
