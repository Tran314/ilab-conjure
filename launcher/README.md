# iLab CONJURE Rust Launcher

This crate is the Rust system tray / macOS menu bar launcher for iLab CONJURE.

It starts the existing Python WebUI backend without a separate terminal window, opens the default browser at `http://127.0.0.1:8787/`, and keeps a tray/menu bar icon alive with these actions:

- Open WebUI
- Open Settings
- History Library
- Check for Updates
- About iLab CONJURE
- Restart WebUI Service
- Quit

The menu labels follow the WebUI language preference when it exists, otherwise
they fall back to the operating system UI language. The About action shows a
native dialog with the app version, open-source project URL, and an update
check button.

The update checker reads the published signed `latest.json` manifest from the
latest GitHub Release, verifies its Ed25519 signatures with the bundled public
key, and compares the local portable/standard/source version with the manifest
version. In a standard macOS app package that includes the helper, Install
Update copies the helper outside the running bundle, exits the launcher, then
downloads and SHA256-verifies the matching DMG, validates the bundle identity,
replaces the installed app with rollback protection, and relaunches it. Standard
macOS apps without the helper and Windows standard App ZIP packages fall back to
opening the installer download. In a portable package, Install Update starts the bundled updater in
automatic mode, exits the current launcher, lets the updater download and
SHA256-verify the matching zip, replace package-managed files while preserving
`data/`, and restart the launcher. Source checkouts do not have a package
updater, so they fall back to opening the release page.

The launcher does not replace the Python WebUI backend. It wraps startup,
process lifetime, browser opening, signed update checking, updater handoff, and
user-facing menu actions. Server logs are written to
`output/webui-outputs/webui-server.log` by default.

The source logo is `assets/rabbit-logo.svg`. The macOS app icon is generated
from this filled rabbit SVG during portable packaging. Windows builds embed
`assets/rabbit-logo.ico` into the launcher executable so File Explorer and
taskbar entries show the rabbit icon instead of the default executable icon.
Runtime tray/menu-bar drawing uses a small template RGBA rabbit icon generated
in Rust, so the executable does not need an SVG renderer.

The launcher also exposes `--verify-update-manifest <path>` for the updater
scripts. It parses the manifest and verifies the signature without starting the
WebUI or creating a tray icon.

## Development

```bash
cargo test
cargo run
```

On Windows the binary uses the GUI subsystem, so double-clicking it does not create a console window.

## Linux first release

Target: Ubuntu 22.04/24.04 x86_64, GNOME X11 or Wayland. Desktop acceptance on
both sessions is a release gate; automated headless tests do not validate tray
visibility. Linux needs GTK 3, libayatana-appindicator3, libxdo3, curl, xdg-utils,
and zenity. GNOME tray actions also need an enabled AppIndicator extension.
GTK initialization, tray creation, and the GTK event loop run on one thread.
AppIndicator library and symbol preflight catches missing/incompatible tray
libraries before the dependency's lazy loader can panic.
If the display, AppIndicator library, or tray host is unavailable, the backend remains available at
its printed URL. Browser-opening failure also leaves the backend available.

The portable archive and installed DEB contain an explicit bundle identity:

```json
{"schema_version":1,"platform":"linux-x86_64","mode":"portable","package":"portable-tar-gz"}
```

The DEB uses `mode: "installed"` and `package: "standard-deb"`. This file is
`bundle-metadata.json` beside the launcher, `app/`, and `python/` directories.
Both modes use `python/bin/python3`, `app/.deps`, and `linux_webui_app:app`.
Missing metadata, runtime, or bundled dependencies is a damaged-package error;
packaged launch never falls back to system Python, `.venv`, or pip. Source
checkouts retain their venv/dependency bootstrap, regardless of directory name.
The installed program tree is never used for data or bytecode writes.

Portable data defaults to writable `data/` beside the launcher. Installed data
uses absolute `$XDG_DATA_HOME/iLab GPT CONJURE`, otherwise
`$HOME/.local/share/iLab GPT CONJURE`. An absolute `ILAB_CONJURE_DATA_DIR` can
override either default. Saved custom input/output paths are preserved. Logs
are `<configured-output-root>/webui-server.log`. The server receives consistent
`APP_LAUNCHER_MODE` (`portable`, `standard`, or `source`),
`ILAB_CONJURE_APP_DIR`, `ILAB_CONJURE_BUNDLE_DIR`, and `ILAB_CONJURE_DATA_DIR`.

```bash
ilab-conjure --status
ilab-conjure --stop
ilab-conjure --no-tray
ilab-conjure --check-updates
ilab-conjure --download-update
```

For a portable bundle, use `./ilab-conjure-launcher` instead of `ilab-conjure`.
Use the same data-directory override for startup and control commands.
`--status` and `--stop` work without a display. Automatic reuse, browser opening,
and status require product/data identity plus a private ownership record whose
instance token and live process identity match the health response. An unrecorded
or spoofed local service is reported as unverified and is not opened automatically.
Manually started source services must be opened manually. The first launcher owns its backend; subsequent
launches reuse a matching service without taking ownership. Explicit tray
Restart/Quit can control a reused launcher-owned backend after verifying its
current health instance against the ownership record. Closing a secondary
launcher without that explicit action does not stop the detached backend.
Stop verifies the
private service record, UID, process start time, launcher token, and server
command. Linux pidfds protect against PID reuse. It sends SIGTERM, waits up to
eight seconds, then uses SIGKILL only for that verified process. Unrelated
services on port 8787 are never terminated. Startup/control errors exit nonzero.

Linux update checks preserve the schema-1 Ed25519 signing protocol and choose
`linux-x86_64` from the portable or standard map. Download uses HTTPS only,
verifies the signed SHA256, and saves into `<data-dir>/downloads/`. Failed or
corrupt partial downloads are removed. Neither update command extracts,
replaces, runs, or installs the package. Install the verified archive or DEB
manually; source checkouts do not offer package replacement.
