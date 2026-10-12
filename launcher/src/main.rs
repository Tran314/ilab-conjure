#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

use anyhow::{Context, Result};
#[cfg(not(target_os = "linux"))]
use ilab_conjure_launcher::UpdateOutcome;
use ilab_conjure_launcher::{
    localized_menu_labels, maybe_offer_legacy_portable_migration, rabbit_icon_rgba,
    resolve_launcher_locale, verify_update_manifest_file, AppLocale, LauncherConfig, WebUiService,
    APP_NAME, WEBUI_URL,
};
use std::path::Path;
use std::time::{Duration, Instant};
use tray_icon::{
    menu::{Menu, MenuEvent, MenuId, MenuItem, PredefinedMenuItem},
    Icon, TrayIcon, TrayIconBuilder,
};
#[cfg(not(target_os = "linux"))]
use winit::{
    event::{Event, StartCause},
    event_loop::{ControlFlow, EventLoop},
};

const MENU_OPEN_WEBUI: &str = "open-webui";
const MENU_OPEN_SETTINGS: &str = "open-settings";
const MENU_OPEN_HISTORY: &str = "open-history";
const MENU_CHECK_UPDATES: &str = "check-updates";
const MENU_ABOUT: &str = "about";
const MENU_RESTART: &str = "restart";
const MENU_QUIT: &str = "quit";
const LOCALE_SYNC_INTERVAL: Duration = Duration::from_secs(3);

#[cfg(not(target_os = "linux"))]
#[derive(Debug)]
enum UserEvent {
    Menu(MenuEvent),
}

struct TrayState {
    _tray_icon: TrayIcon,
    _menu: Menu,
    open_item: MenuItem,
    settings_item: MenuItem,
    history_item: MenuItem,
    update_item: MenuItem,
    about_item: MenuItem,
    restart_item: MenuItem,
    quit_item: MenuItem,
    _separators: Vec<PredefinedMenuItem>,
    locale: AppLocale,
}

impl TrayState {
    fn sync_locale(&mut self, locale: AppLocale) {
        if self.locale == locale {
            return;
        }
        let labels = localized_menu_labels(locale);
        self.open_item.set_text(labels.open_webui);
        self.settings_item.set_text(labels.open_settings);
        self.history_item.set_text(labels.open_history);
        self.update_item.set_text(labels.check_updates);
        self.about_item.set_text(labels.about);
        self.restart_item.set_text(labels.restart);
        self.quit_item.set_text(labels.quit);
        self.locale = locale;
    }
}

fn main() {
    if let Err(error) = run() {
        show_startup_error(&format!("{error:#}"));
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("--verify-update-manifest") {
        if args.len() != 2 {
            anyhow::bail!("--verify-update-manifest requires exactly one manifest path");
        }
        return verify_update_manifest_file(Path::new(&args[1]));
    }
    if args == ["--help"] || args == ["-h"] {
        println!("iLab CONJURE launcher\n  --status          Show matching service status\n  --stop            Gracefully stop this data directory's launcher-owned service (Linux)\n  --no-tray         Start/open WebUI without a tray; control with --status/--stop\n  --check-updates   Verify signed manifest and show latest version\n  --download-update Download and SHA256-verify Linux package; install manually\n  --verify-update-manifest PATH\n  --version");
        return Ok(());
    }
    if args == ["--version"] {
        println!("{}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if args.len() > 1
        || args.first().is_some_and(|a| {
            !matches!(
                a.as_str(),
                "--status" | "--stop" | "--no-tray" | "--check-updates" | "--download-update"
            )
        })
    {
        anyhow::bail!("Unknown arguments; use --help");
    }
    let config = LauncherConfig::detect()?;
    let mut service = WebUiService::new(config.clone());
    match args.first().map(String::as_str) {
        Some("--status") => {
            println!("{}", service.status()?);
            return Ok(());
        }
        Some("--stop") => {
            service.stop_recorded_service()?;
            println!("Owned service stopped.");
            return Ok(());
        }
        Some("--check-updates") => {
            println!("{}", service.update_cli(false)?);
            return Ok(());
        }
        Some("--download-update") => {
            println!("{}", service.update_cli(true)?);
            return Ok(());
        }
        Some("--no-tray") => {
            service.ensure_running()?;
            if let Err(error) = service.open_webui() {
                eprintln!("Browser unavailable: {error:#}. Open {}", config.url());
            }
            service.detach_service();
            println!(
                "WebUI available at {}. Use --status / --stop to control it.",
                config.url()
            );
            return Ok(());
        }
        _ => {}
    }
    maybe_offer_legacy_portable_migration(
        &config,
        resolve_launcher_locale(&config.webui_settings_path()),
    )?;
    run_desktop(config)
}

// winit is retained for macOS/Windows. Linux uses GTK for both initialization
// and the event loop, on this same main thread.
#[cfg(not(target_os = "linux"))]
#[allow(deprecated)]
fn run_desktop(config: LauncherConfig) -> Result<()> {
    let mut service = WebUiService::new(config.clone());
    let event_loop = EventLoop::<UserEvent>::with_user_event()
        .build()
        .context("failed to create launcher event loop")?;
    let proxy = event_loop.create_proxy();
    MenuEvent::set_event_handler(Some(move |event| {
        let _ = proxy.send_event(UserEvent::Menu(event));
    }));

    let mut tray_state: Option<TrayState> = None;
    let mut opened_on_startup = false;
    let mut next_locale_sync = Instant::now() + LOCALE_SYNC_INTERVAL;

    event_loop.run(move |event, target| {
        target.set_control_flow(ControlFlow::WaitUntil(next_locale_sync));

        match event {
            Event::NewEvents(StartCause::Init) => {
                if tray_state.is_none() {
                    match create_tray_state(&config) {
                        Ok(state) => tray_state = Some(state),
                        Err(error) => {
                            show_startup_error(&format!(
                                "Could not create system tray icon: {error:#}"
                            ));
                            target.exit();
                        }
                    }
                }
                if !opened_on_startup {
                    opened_on_startup = true;
                    if let Err(error) = service.ensure_running().and_then(|_| service.open_webui())
                    {
                        let _ = service.log_line(&format!("startup failed: {error:#}"));
                        show_startup_error(&format!("{error:#}"));
                    }
                }
            }
            Event::AboutToWait => {
                if Instant::now() >= next_locale_sync {
                    if let Some(state) = tray_state.as_mut() {
                        state.sync_locale(resolve_launcher_locale(&config.webui_settings_path()));
                    }
                    next_locale_sync = Instant::now() + LOCALE_SYNC_INTERVAL;
                    target.set_control_flow(ControlFlow::WaitUntil(next_locale_sync));
                }
            }
            Event::UserEvent(UserEvent::Menu(event)) => match event.id.as_ref() {
                MENU_OPEN_WEBUI => {
                    if let Err(error) = service.ensure_running().and_then(|_| service.open_webui())
                    {
                        show_startup_error(&format!("{error:#}"));
                    }
                }
                MENU_OPEN_SETTINGS => {
                    if let Err(error) = service
                        .ensure_running()
                        .and_then(|_| service.open_settings())
                    {
                        show_startup_error(&format!("{error:#}"));
                    }
                }
                MENU_OPEN_HISTORY => {
                    if let Err(error) = service
                        .ensure_running()
                        .and_then(|_| service.open_history())
                    {
                        show_startup_error(&format!("{error:#}"));
                    }
                }
                MENU_CHECK_UPDATES => {
                    let locale = tray_state
                        .as_ref()
                        .map(|state| state.locale)
                        .unwrap_or_else(|| resolve_launcher_locale(&config.webui_settings_path()));
                    match service.check_for_updates(locale) {
                        Ok(UpdateOutcome::Continue) => {}
                        Ok(UpdateOutcome::LaunchedUpdater) => {
                            service.stop_owned_service();
                            target.exit();
                        }
                        Err(error) => show_startup_error(&format!("{error:#}")),
                    }
                }
                MENU_ABOUT => {
                    let locale = tray_state
                        .as_ref()
                        .map(|state| state.locale)
                        .unwrap_or_else(|| resolve_launcher_locale(&config.webui_settings_path()));
                    match service.show_about(locale) {
                        Ok(UpdateOutcome::Continue) => {}
                        Ok(UpdateOutcome::LaunchedUpdater) => {
                            service.stop_owned_service();
                            target.exit();
                        }
                        Err(error) => show_startup_error(&format!("{error:#}")),
                    }
                }
                MENU_RESTART => {
                    if let Err(error) = service.restart_owned_service() {
                        show_startup_error(&format!("{error:#}"));
                    }
                }
                MENU_QUIT => {
                    service.stop_owned_service();
                    target.exit();
                }
                _ => {}
            },
            Event::LoopExiting => {
                service.stop_owned_service();
            }
            _ => {}
        }
    })?;

    Ok(())
}

#[cfg(target_os = "linux")]
fn run_desktop(config: LauncherConfig) -> Result<()> {
    use gtk::glib;
    use std::{cell::RefCell, rc::Rc};

    let mut service = WebUiService::new(config.clone());
    service.ensure_running()?;
    if let Err(error) = service.open_webui() {
        let _ = service.log_line(&format!(
            "Browser unavailable: {error:#}. Open {}",
            config.url()
        ));
        eprintln!("Browser unavailable: {error:#}. Open {}", config.url());
    }
    let tray = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<_> {
        let library = ilab_conjure_launcher::linux_tray::preflight_appindicator()?;
        gtk::init().map_err(|error| anyhow::anyhow!("{error}"))?;
        if !linux_tray_host_available() {
            anyhow::bail!("No StatusNotifier tray host is available (GNOME may need its AppIndicator extension)");
        }
        Ok((create_tray_state(&config)?, library))
    })).unwrap_or_else(|_| Err(anyhow::anyhow!("Desktop tray initialization panicked")));
    let (tray, _indicator_library) = match tray {
        Ok(tray) => tray,
        Err(error) => {
            let message = format!("Tray unavailable: {error:#}. WebUI remains available at {}. Use ilab-conjure --status or --stop to control it. Log: {}", config.url(), config.log_path.display());
            let _ = service.log_line(&message);
            service.detach_service();
            show_startup_error(&message);
            return Ok(());
        }
    };
    let service = Rc::new(RefCell::new(service));
    let tray = Rc::new(RefCell::new(tray));
    let failure: Rc<RefCell<Option<String>>> = Rc::new(RefCell::new(None));
    for signal in [libc::SIGTERM, libc::SIGINT] {
        let service = service.clone();
        glib::source::unix_signal_add_local(signal, move || {
            service.borrow_mut().stop_owned_service();
            gtk::main_quit();
            glib::ControlFlow::Break
        });
    }
    let loop_service = service.clone();
    let loop_tray = tray.clone();
    let loop_failure = failure.clone();
    let mut next_locale_sync = Instant::now() + LOCALE_SYNC_INTERVAL;
    let timer = glib::timeout_add_local(Duration::from_millis(200), move || {
        if Instant::now() >= next_locale_sync {
            loop_tray
                .borrow_mut()
                .sync_locale(resolve_launcher_locale(&config.webui_settings_path()));
            next_locale_sync = Instant::now() + LOCALE_SYNC_INTERVAL;
        }
        let mut service = loop_service.borrow_mut();
        match service.owned_child_exited() {
            Ok(true) => {
                gtk::main_quit();
                return glib::ControlFlow::Continue;
            }
            Err(error) => {
                *loop_failure.borrow_mut() = Some(format!("{error:#}"));
                gtk::main_quit();
                return glib::ControlFlow::Continue;
            }
            _ => {}
        }
        while let Ok(event) = MenuEvent::receiver().try_recv() {
            let locale = loop_tray.borrow().locale;
            let result: Result<bool> = (|| {
                match event.id.as_ref() {
                    MENU_OPEN_WEBUI => {
                        service.ensure_running()?;
                        service.open_webui()?;
                    }
                    MENU_OPEN_SETTINGS => {
                        service.ensure_running()?;
                        service.open_settings()?;
                    }
                    MENU_OPEN_HISTORY => {
                        service.ensure_running()?;
                        service.open_history()?;
                    }
                    MENU_CHECK_UPDATES => {
                        service.check_for_updates(locale)?;
                    }
                    MENU_ABOUT => {
                        service.show_about(locale)?;
                    }
                    MENU_RESTART => {
                        service.restart_owned_service()?;
                    }
                    MENU_QUIT => {
                        service.stop_requested_service()?;
                        return Ok(true);
                    }
                    _ => {}
                }
                Ok(false)
            })();
            match result {
                Ok(true) => {
                    gtk::main_quit();
                    break;
                }
                Err(error) => show_startup_error(&format!("{error:#}")),
                _ => {}
            }
        }
        glib::ControlFlow::Continue
    });
    gtk::main();
    timer.remove();
    service.borrow_mut().stop_owned_service();
    let error = failure.borrow_mut().take();
    if let Some(message) = error {
        anyhow::bail!(message);
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn linux_tray_host_available() -> bool {
    std::process::Command::new("gdbus")
        .args([
            "call",
            "--session",
            "--timeout",
            "2",
            "--dest",
            "org.freedesktop.DBus",
            "--object-path",
            "/org/freedesktop/DBus",
            "--method",
            "org.freedesktop.DBus.NameHasOwner",
            "org.kde.StatusNotifierWatcher",
        ])
        .output()
        .is_ok_and(|output| {
            output.status.success() && String::from_utf8_lossy(&output.stdout).contains("true")
        })
}

fn create_tray_state(config: &LauncherConfig) -> Result<TrayState> {
    let locale = resolve_launcher_locale(&config.webui_settings_path());
    let labels = localized_menu_labels(locale);
    let menu = Menu::new();
    let open_item = MenuItem::with_id(MenuId::new(MENU_OPEN_WEBUI), labels.open_webui, true, None);
    let settings_item = MenuItem::with_id(
        MenuId::new(MENU_OPEN_SETTINGS),
        labels.open_settings,
        true,
        None,
    );
    let history_item = MenuItem::with_id(
        MenuId::new(MENU_OPEN_HISTORY),
        labels.open_history,
        true,
        None,
    );
    let update_item = MenuItem::with_id(
        MenuId::new(MENU_CHECK_UPDATES),
        labels.check_updates,
        true,
        None,
    );
    let about_item = MenuItem::with_id(MenuId::new(MENU_ABOUT), labels.about, true, None);
    let restart_item = MenuItem::with_id(MenuId::new(MENU_RESTART), labels.restart, true, None);
    let quit_item = MenuItem::with_id(MenuId::new(MENU_QUIT), labels.quit, true, None);
    let separator_one = PredefinedMenuItem::separator();
    let separator_two = PredefinedMenuItem::separator();

    menu.append_items(&[
        &open_item,
        &settings_item,
        &history_item,
        &update_item,
        &about_item,
        &separator_one,
        &restart_item,
        &separator_two,
        &quit_item,
    ])?;

    let (rgba, width, height) = rabbit_icon_rgba(32);
    let icon = Icon::from_rgba(rgba, width, height).context("failed to build rabbit tray icon")?;
    let tray_icon = TrayIconBuilder::new()
        .with_tooltip(format!("{APP_NAME} - {WEBUI_URL}"))
        .with_menu(Box::new(menu.clone()))
        .with_icon(icon)
        .with_icon_as_template(true)
        .with_menu_on_left_click(true)
        .with_menu_on_right_click(true)
        .build()
        .context("failed to create tray icon")?;

    Ok(TrayState {
        _tray_icon: tray_icon,
        _menu: menu,
        open_item,
        settings_item,
        history_item,
        update_item,
        about_item,
        restart_item,
        quit_item,
        _separators: vec![separator_one, separator_two],
        locale,
    })
}

fn show_startup_error(message: &str) {
    #[cfg(target_os = "macos")]
    {
        let _ = std::process::Command::new("osascript")
            .arg("-e")
            .arg(format!(
                "display dialog {:?} with title {:?} buttons {{\"OK\"}} default button \"OK\"",
                message, APP_NAME
            ))
            .status();
    }

    #[cfg(target_os = "windows")]
    {
        let _ = std::process::Command::new("powershell")
            .args([
                "-NoProfile",
                "-Command",
                &format!(
                    "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show({:?}, {:?})",
                    message, APP_NAME
                ),
            ])
            .status();
    }

    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    {
        eprintln!("{APP_NAME}: {message}");
        // Desktop launches have no terminal. stderr remains the fallback in headless use.
        if std::env::var_os("DISPLAY").is_some() || std::env::var_os("WAYLAND_DISPLAY").is_some() {
            let _ = std::process::Command::new("zenity")
                .args(["--warning", "--title", APP_NAME, "--text", message])
                .status();
        }
    }
}
