# First Linux release contract

The initial target is **Ubuntu 22.04 and Ubuntu 24.04, x86_64, GNOME, X11 and
Wayland**. Broader distributions, desktop environments, architectures, AppImage,
Flatpak, and system Python support are outside this first package scope.

Packages contain the native Rust tray launcher, the prebuilt WebUI and the same
pinned Python runtime/dependencies. End users do not install Python, Node.js or
Rust. Build on Ubuntu 22.04 to retain the oldest supported glibc baseline.

- [Portable install/upgrade](../packaging/linux/README-portable.md): extract
  `ilab-gpt-conjure_linux_portable_x64_VERSION.tar.gz` into a writable directory.
- [Installed DEB install/upgrade](../packaging/linux/README-app.md): install
  `iLab-GPT-CONJURE-linux-x64_VERSION.deb` using `sudo apt install ./FILE.deb`.
- [Third-party notices and runtime provenance](../packaging/linux/THIRD_PARTY_NOTICES.md)

## Build

Maintainers need Python 3.11.8+, Node.js 22, Rust stable, curl/CA trust, dpkg-deb,
build-essential, pkg-config, libgtk-3-dev, libayatana-appindicator3-dev, and libxdo-dev. Use a
clean checkout and the locked dependencies. Local builds on newer Linux hosts
can require a newer glibc and are development artifacts, not Ubuntu 22.04 releases.
Release CI rejects any packaged ELF requiring glibc newer than 2.35:

```sh
./packaging/linux/build-portable.sh --version 0.9.7 --output-dir .dist
./packaging/linux/build-app.sh --version 0.9.7 --output-dir .dist \
  --portable-dir .dist/ilab-gpt-conjure_linux_portable_x64
python3 packaging/linux/verify-runtime.py --bundle .dist/ilab-gpt-conjure_linux_portable_x64 --max-glibc 2.35
```

`build-app.sh` without `--portable-dir` builds the portable payload first. Both
formats use precisely that payload. The DEB changes only installation metadata,
entry points and version marker; it contains no `data/`. Build manifests and
SHA256 sidecars accompany both archives. This command does not publish a release.

The PBS release, CPython version, asset, target triple and SHA256 are fixed in
`packaging/linux/python-runtime.json`. It has no `latest` download fallback and no
checksum override. Updates to this pin require matching the official release API
asset digest and published SHA256SUMS before changing it. Every build verifies the
cached/downloaded bytes before unpacking. Python dependencies are installed with
`--require-hashes --only-binary=:all:`. Build tools, frontend source/maps, pip and
tests are removed while licenses, certificates, static assets, runtime libraries
and package metadata remain.

## Runtime identity and data boundary

`bundle-metadata.json` has schema 1, `platform: linux-x86_64`, and an explicit
`mode: portable` or `mode: installed`. Its package field is `portable-tar-gz` or
`standard-deb`. The launcher uses this identity, not heuristics about a nearby
`data/` directory. A source checkout has no marker.

Both bundles use `python/bin/python3`, `app/linux_webui_app.py` and
`ilab-conjure-launcher` at the root. `APP_LAUNCHER_MODE` is `portable` or `standard`;
`ILAB_CONJURE_BUNDLE_DIR`, `ILAB_CONJURE_APP_DIR`, and `ILAB_CONJURE_DATA_DIR` describe
the resolved paths. A standard bundle reporting BUNDLE_DIR remains standard.

The installed program lives at `/opt/ilab-conjure`. Only an absolute, nonempty
`XDG_DATA_HOME` is used; otherwise the default is `~/.local/share`. Append the
legacy product directory `iLab GPT CONJURE`. Portable defaults to bundle `data/`.
Settings, auth, SQLite, inputs, outputs, thumbnails, backups, logs and locks must
not write under installed program files. Saved custom storage roots are loaded
instead of reset. The DEB has no maintainer script that creates or removes data.

## Update and release contract

`latest.json` stays schema 1. The existing portable signature covers the Linux
`platforms.linux-x86_64` tar.gz entry; `standard_signature` covers the DEB entry in
`standard_platforms.linux-x86_64`. The canonical signed fields and sort order are
unchanged. Missing, duplicate, wrong-version or checksum-invalid assets fail manifest generation.
PR CI and release CI build on Ubuntu 22.04, enforce the glibc baseline, then
install the DEB and test both formats on Ubuntu 22.04/24.04. Headless lifecycle
checks use a disposable profile to start/reopen/status/stop, preserve custom paths
and confirm the program tree is untouched. The release job waits for it, discovers tar.gz/DEB artifacts,
and includes them in the final asset whitelist.

Linux v1 checks signed metadata and offers the appropriate package download. It
never auto-replaces a package, runs a macOS `.command` updater, invokes apt/sudo,
or imports Windows/macOS installation assumptions. Quit, back up data, install a
complete verified package, and restart. Keep the old data until the new version
has successfully opened your settings/history and saved images.

## Native desktop dependencies

The DEB depends on `libgtk-3-0t64 | libgtk-3-0` and
`libayatana-appindicator3-1 | libappindicator3-1`, plus standard X11/Wayland and
browser-opening runtime libraries (including `libxdo3`), `curl` and `ca-certificates` for update checks, `zenity` for graphical error
dialogs, and `libglib2.0-bin` for session-bus/tray-host discovery. Ubuntu's `libayatana-appindicator3-1` name is
unchanged in 24.04 while GTK changes to `libgtk-3-0t64`.

GTK's `libgtk-3.so.0` and AppIndicator's `libayatana-appindicator3.so.1` or
`libappindicator3.so.1` are loaded dynamically. `ldd` alone cannot prove these
libraries exist. `verify-runtime.py` attempts their actual dlopen and checks the
bundled Python and launcher ELF dependencies. Even successful library checks and
headless HTTP smoke tests do not establish a working tray or browser handoff.
GNOME needs the AppIndicator extension enabled, a session bus and a display.

Package references: [Ubuntu 24.04 Ayatana](https://packages.ubuntu.com/noble/libayatana-appindicator3-1),
[Ubuntu 22.04 Ayatana](https://packages.ubuntu.com/jammy/libayatana-appindicator3-1),
[Ubuntu 24.04 GTK](https://packages.ubuntu.com/noble/libgtk-3-0t64).

## Real-session qualification: all pending

Do not mark these passed based on a headless container, Xvfb or unit tests. Record
VM/hardware, OS build, session type, package checksum and actual observations.

| Ubuntu | Session | Package | Status |
| --- | --- | --- | --- |
| 22.04 | GNOME X11 | portable tar.gz | PENDING |
| 22.04 | GNOME Wayland | portable tar.gz | PENDING |
| 22.04 | GNOME X11 | installed DEB | PENDING |
| 22.04 | GNOME Wayland | installed DEB | PENDING |
| 24.04 | GNOME X11 | portable tar.gz | PENDING |
| 24.04 | GNOME Wayland | portable tar.gz | PENDING |
| 24.04 | GNOME X11 | installed DEB | PENDING |
| 24.04 | GNOME Wayland | installed DEB | PENDING |

For **each of the eight rows**:

- [ ] Clean install/extract on an account without Python/Node/Rust developer tools
- [ ] First launch, visible tray, correct icon and browser handoff; actionable
      diagnostics if a library, session bus, display, or tray support is missing
- [ ] Tray Open WebUI / Settings / History / About; quit and restart cleanly
- [ ] Starting twice focuses/reopens the existing instance without another backend
- [ ] Two deliberately separate profiles can coexist without port/lock confusion
- [ ] Stale lock/PID and occupied-port paths recover or report an accurate error
- [ ] Settings/auth/task/history/gallery/thumbnail/backup/logs persist after restart
- [ ] Installed `/opt` tree stays unchanged and unwritable as a normal user; test
      unset, absolute and relative XDG_DATA_HOME and retained custom storage roots
- [ ] Loopback-only access, authenticated writes and CSRF checks remain intact
- [ ] Generation cancel and retry, gallery reference delete/undo, and backup restore
      succeed without data loss or unexpected path resets
- [ ] Update check offers the correct signed tar.gz or DEB; no automatic replacement
- [ ] Quit, upgrade, restart; settings/history/images survive; removal keeps data

Use a disposable profile for fault injection. No production release should claim
these desktop scenarios are verified until the observations are recorded.
