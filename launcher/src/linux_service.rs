//! Linux control of only the backend launched for this data directory.
use crate::{bundle, LauncherConfig};
use anyhow::{anyhow, bail, Context, Result};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd, OwnedFd},
        unix::fs::{MetadataExt, OpenOptionsExt},
    },
    path::PathBuf,
    thread,
    time::{Duration, Instant},
};

const RECORD: &str = ".launcher-service.json";
const STOP_TIMEOUT: Duration = Duration::from_secs(8);

pub(crate) fn lock(config: &LauncherConfig) -> Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(config.data_dir.join(".launcher-control.lock"))?;
    // Never wait forever for a stuck launcher. The startup health timeout is 30s.
    let start = Instant::now();
    loop {
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
            return Ok(file);
        }
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::EWOULDBLOCK) {
            return Err(error.into());
        }
        if start.elapsed() > Duration::from_secs(40) {
            bail!("Another launcher is busy; try --status or --stop shortly");
        }
        thread::sleep(Duration::from_millis(100));
    }
}

pub(crate) fn new_instance() -> Result<String> {
    let mut bytes = [0u8; 32];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn start_time(pid: u32) -> Result<String> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat"))?;
    // The comm field can contain spaces and parentheses. starttime is field 22.
    stat.rsplit_once(')')
        .and_then(|(_, tail)| tail.split_whitespace().nth(19))
        .map(str::to_string)
        .ok_or_else(|| anyhow!("Malformed process identity"))
}

pub(crate) fn write_record(config: &LauncherConfig, pid: u32, instance: &str) -> Result<()> {
    let value = serde_json::json!({"pid":pid,"start_time":start_time(pid)?,"instance":instance,
        "data_dir":config.data_dir,"app_dir":config.app_dir,"port":config.port});
    let path = config
        .data_dir
        .join(format!(".launcher-service-{}.tmp", std::process::id()));
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)?;
        file.write_all(serde_json::to_string(&value)?.as_bytes())?;
        file.sync_all()?;
        fs::rename(&path, config.data_dir.join(RECORD))?;
        Ok(())
    })();
    let _ = fs::remove_file(path);
    result
}

fn read_record(config: &LauncherConfig) -> Result<serde_json::Value> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(config.data_dir.join(RECORD))
        .context("No launcher-owned service record exists for this data directory")?;
    let metadata = file.metadata()?;
    if metadata.uid() != unsafe { libc::geteuid() } || metadata.mode() & 0o077 != 0 {
        bail!("Refusing service control: ownership record is not private to the current user");
    }
    let mut text = String::new();
    (&mut file).take(16_384).read_to_string(&mut text)?;
    let value: serde_json::Value = serde_json::from_str(&text)?;
    if value["data_dir"].as_str() != config.data_dir.to_str()
        || value["app_dir"].as_str() != config.app_dir.to_str()
        || value["port"].as_u64() != Some(config.port as u64)
    {
        bail!("Refusing service control: service belongs to a different app or data directory");
    }
    Ok(value)
}

pub(crate) fn remove_record(config: &LauncherConfig, instance: &str) {
    if let Ok(value) = read_record(config) {
        if value["instance"].as_str() == Some(instance) {
            let _ = fs::remove_file(config.data_dir.join(RECORD));
        }
    }
}

fn verified_process(config: &LauncherConfig, value: &serde_json::Value) -> Result<(u32, OwnedFd)> {
    let pid = value["pid"]
        .as_u64()
        .filter(|v| *v > 1 && *v <= i32::MAX as u64)
        .ok_or_else(|| anyhow!("Invalid owned service PID"))? as u32;
    // pidfd pins process identity, avoiding PID reuse between validation and signals.
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error()).context("Owned service is no longer running");
    }
    let fd = unsafe { OwnedFd::from_raw_fd(fd as i32) };
    let proc_dir = PathBuf::from(format!("/proc/{pid}"));
    if fs::metadata(&proc_dir)?.uid() != unsafe { libc::geteuid() }
        || value["start_time"].as_str() != Some(start_time(pid)?.as_str())
    {
        bail!("Refusing service control: process owner or identity changed");
    }
    let instance = value["instance"]
        .as_str()
        .filter(|v| v.len() == 64)
        .ok_or_else(|| anyhow!("Invalid service ownership token"))?;
    let environment = fs::read(proc_dir.join("environ"))?;
    for expected in [
        format!("ILAB_CONJURE_LAUNCHER_INSTANCE={instance}"),
        format!("ILAB_CONJURE_DATA_DIR={}", config.data_dir.display()),
        format!("ILAB_CONJURE_APP_DIR={}", config.app_dir.display()),
    ] {
        if !environment
            .split(|byte| *byte == 0)
            .any(|entry| entry == expected.as_bytes())
        {
            bail!("Refusing service control: process was not started by this launcher");
        }
    }
    let cmdline = fs::read(proc_dir.join("cmdline"))?;
    if !cmdline
        .split(|v| *v == 0)
        .any(|arg| arg == b"codex_image.webui.server")
    {
        bail!("Refusing service control: process is not the WebUI server");
    }
    Ok((pid, fd))
}

fn exited(fd: &OwnedFd, timeout: i32) -> Result<bool> {
    let mut pfd = libc::pollfd {
        fd: fd.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    let result = unsafe { libc::poll(&mut pfd, 1, timeout) };
    if result < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(result > 0)
}

pub(crate) fn stop_recorded(config: &LauncherConfig) -> Result<()> {
    stop_recorded_with_timeout(config, STOP_TIMEOUT, None)
}

pub(crate) fn stop_matching_recorded(
    config: &LauncherConfig,
    expected_instance: &str,
) -> Result<()> {
    stop_recorded_with_timeout(config, STOP_TIMEOUT, Some(expected_instance))
}

fn stop_recorded_with_timeout(
    config: &LauncherConfig,
    timeout: Duration,
    expected_instance: Option<&str>,
) -> Result<()> {
    let _lock = lock(config)?;
    let value = read_record(config)?;
    if let Some(expected) = expected_instance {
        if expected.is_empty() || value["instance"].as_str() != Some(expected) {
            bail!(
                "Refusing service control: current WebUI does not match the launcher-owned process"
            );
        }
    }
    let (_pid, fd) = verified_process(config, &value)?;
    for signal in [libc::SIGTERM, libc::SIGKILL] {
        if exited(&fd, 0)? {
            break;
        }
        if unsafe {
            libc::syscall(
                libc::SYS_pidfd_send_signal,
                fd.as_raw_fd(),
                signal,
                std::ptr::null::<libc::siginfo_t>(),
                0,
            )
        } < 0
        {
            return Err(std::io::Error::last_os_error().into());
        }
        if exited(&fd, timeout.as_millis() as i32)? {
            break;
        }
        if signal == libc::SIGKILL {
            bail!("Owned service did not stop after SIGKILL");
        }
    }
    remove_record(config, value["instance"].as_str().unwrap_or_default());
    Ok(())
}

pub(crate) fn verify_health_owner(
    config: &LauncherConfig,
    health: &serde_json::Value,
) -> Result<()> {
    let value =
        read_record(config).context("Unverified service: no trusted launcher ownership record")?;
    let instance = health
        .get("launcher_instance")
        .and_then(|v| v.as_str())
        .filter(|v| v.len() == 64)
        .ok_or_else(|| anyhow!("Unverified service: missing launcher instance identity"))?;
    if value["instance"].as_str() != Some(instance) {
        bail!("Unverified service: health instance does not match launcher ownership record");
    }
    verified_process(config, &value)
        .context("Unverified service: launcher-owned process identity cannot be verified")?;
    Ok(())
}

pub(crate) fn owned_status(config: &LauncherConfig) -> bool {
    read_record(config)
        .and_then(|value| verified_process(config, &value))
        .is_ok()
}

pub(crate) fn verify_writable(config: &LauncherConfig) -> Result<()> {
    if config.mode == crate::BundleMode::Installed
        && bundle::absolute_path(&config.data_dir)?.starts_with(&config.bundle_dir)
    {
        bail!("Installed program directory is read-only; choose a user data directory");
    }
    fs::create_dir_all(&config.data_dir).with_context(|| {
        format!(
            "Data directory is not writable: {}",
            config.data_dir.display()
        )
    })?;
    let data = bundle::absolute_path(&config.data_dir)?;
    if config.mode == crate::BundleMode::Installed && data.starts_with(&config.bundle_dir) {
        bail!(
            "Installed program directory is read-only; select a user data directory outside {}",
            config.bundle_dir.display()
        );
    }
    let probe = data.join(format!(".launcher-write-probe-{}", new_instance()?));
    let result = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&probe);
    if result.is_ok() {
        fs::remove_file(&probe)?;
    }
    result.with_context(|| format!("Data directory is not writable: {}. Move the portable package to a writable folder or set ILAB_CONJURE_DATA_DIR.", data.display()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::{Child, Command, Stdio};
    fn fixture(name: &str, script: &str) -> (PathBuf, LauncherConfig, Child, String) {
        let root = std::env::temp_dir().join(format!("ilab-control-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("source")).unwrap();
        let config =
            LauncherConfig::from_dirs(root.join("source"), root.join("data"), 18787).unwrap();
        fs::create_dir_all(&config.data_dir).unwrap();
        let instance = new_instance().unwrap();
        let child = Command::new("sh")
            .args(["-c", script, "codex_image.webui.server"])
            .env("ILAB_CONJURE_DATA_DIR", &config.data_dir)
            .env("ILAB_CONJURE_APP_DIR", &config.app_dir)
            .env("ILAB_CONJURE_LAUNCHER_INSTANCE", &instance)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        write_record(&config, child.id(), &instance).unwrap();
        (root, config, child, instance)
    }
    #[test]
    fn recorded_control_gracefully_stops_only_verified_process() {
        let (root, config, mut child, _) =
            fixture("term", "trap 'exit 0' TERM; while :; do sleep 0.05; done");
        // Wait for shell to install its handler before exercising graceful shutdown.
        thread::sleep(Duration::from_millis(100));
        assert!(owned_status(&config));
        stop_recorded(&config).unwrap();
        assert!(child.wait().unwrap().success());
        assert!(!config.data_dir.join(RECORD).exists());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn stale_or_tampered_ownership_never_signals_process() {
        let (root, config, mut child, instance) =
            fixture("foreign", "while :; do sleep 0.05; done");
        let mut record = read_record(&config).unwrap();
        record["instance"] = serde_json::json!("a".repeat(64));
        fs::write(config.data_dir.join(RECORD), record.to_string()).unwrap();
        assert!(stop_recorded(&config).is_err());
        assert!(child.try_wait().unwrap().is_none());
        record["instance"] = serde_json::json!(instance);
        record["start_time"] = serde_json::json!("0");
        fs::write(config.data_dir.join(RECORD), record.to_string()).unwrap();
        assert!(stop_recorded(&config).is_err());
        assert!(child.try_wait().unwrap().is_none());
        child.kill().unwrap();
        child.wait().unwrap();
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn record_permissions_and_data_identity_are_required() {
        use std::os::unix::fs::PermissionsExt;
        let (root, config, mut child, _) = fixture("permissions", "while :; do sleep 0.05; done");
        fs::set_permissions(
            config.data_dir.join(RECORD),
            fs::Permissions::from_mode(0o644),
        )
        .unwrap();
        assert!(stop_recorded(&config).is_err());
        assert!(child.try_wait().unwrap().is_none());
        fs::set_permissions(
            config.data_dir.join(RECORD),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        let mut record = read_record(&config).unwrap();
        record["data_dir"] = serde_json::json!("/unrelated");
        fs::write(config.data_dir.join(RECORD), record.to_string()).unwrap();
        assert!(stop_recorded(&config).is_err());
        assert!(child.try_wait().unwrap().is_none());
        child.kill().unwrap();
        child.wait().unwrap();
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn verified_process_ignoring_term_is_killed_after_timeout() {
        use std::os::unix::process::ExitStatusExt;
        let (root, config, mut child, _) =
            fixture("kill", "trap '' TERM; while :; do sleep 0.05; done");
        thread::sleep(Duration::from_millis(100));
        stop_recorded_with_timeout(&config, Duration::from_millis(200), None).unwrap();
        assert_eq!(child.wait().unwrap().signal(), Some(libc::SIGKILL));
        assert!(!config.data_dir.join(RECORD).exists());
        fs::remove_dir_all(root).unwrap();
    }
    fn one_health_response(config: &mut LauncherConfig, instance: &str) -> thread::JoinHandle<()> {
        use std::net::TcpListener;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        config.port = listener.local_addr().unwrap().port();
        let body = serde_json::json!({"ok":true,"product":"ilab-conjure","data_dir":config.data_dir,"launcher_instance":instance}).to_string();
        thread::spawn(move || loop {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 1024];
            if stream.read(&mut request).unwrap() == 0 {
                continue;
            }
            stream
                .write_all(
                    format!(
                        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    )
                    .as_bytes(),
                )
                .unwrap();
            break;
        })
    }

    #[test]
    fn passive_secondary_launcher_drop_leaves_detached_backend_running() {
        let (root, config, mut child, _) =
            fixture("passive-secondary", "while :; do sleep 0.05; done");
        // Model an already-ready detached service, after its exec environment is visible.
        let ready = Instant::now();
        while !owned_status(&config) && ready.elapsed() < Duration::from_secs(2) {
            thread::sleep(Duration::from_millis(10));
        }
        assert!(owned_status(&config));
        drop(crate::WebUiService::new(config.clone()));
        assert!(child.try_wait().unwrap().is_none());
        assert!(owned_status(&config));
        child.kill().unwrap();
        child.wait().unwrap();
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn explicit_quit_controls_matching_detached_backend() {
        let (root, mut config, mut child, instance) = fixture(
            "requested-quit",
            "trap 'exit 0' TERM; while :; do sleep 0.05; done",
        );
        thread::sleep(Duration::from_millis(100));
        let responder = one_health_response(&mut config, &instance);
        write_record(&config, child.id(), &instance).unwrap();
        crate::WebUiService::new(config.clone())
            .stop_requested_service()
            .unwrap();
        responder.join().unwrap();
        assert!(child.wait().unwrap().success());
        assert!(!config.data_dir.join(RECORD).exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn explicit_restart_stops_detached_backend_before_runtime_startup() {
        let (root, mut config, mut child, instance) = fixture(
            "requested-restart",
            "trap 'exit 0' TERM; while :; do sleep 0.05; done",
        );
        thread::sleep(Duration::from_millis(100));
        let responder = one_health_response(&mut config, &instance);
        write_record(&config, child.id(), &instance).unwrap();
        config.mode = crate::BundleMode::Portable;
        let error = crate::WebUiService::new(config.clone())
            .restart_owned_service()
            .unwrap_err();
        responder.join().unwrap();
        assert!(child.wait().unwrap().success());
        // Reaching runtime bootstrap proves Restart did not merely reuse the old backend.
        assert!(error.to_string().contains("Package damaged"));
        assert!(!config.data_dir.join(RECORD).exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn explicit_tray_action_rejects_foreign_health_instance() {
        let (root, mut config, mut child, instance) =
            fixture("foreign-health", "while :; do sleep 0.05; done");
        let responder = one_health_response(&mut config, &"f".repeat(64));
        write_record(&config, child.id(), &instance).unwrap();
        let error = crate::WebUiService::new(config.clone())
            .stop_requested_service()
            .unwrap_err();
        responder.join().unwrap();
        assert!(error.to_string().contains("does not match"));
        assert!(child.try_wait().unwrap().is_none());
        child.kill().unwrap();
        child.wait().unwrap();
        fs::remove_dir_all(root).unwrap();
    }
}
