# Linux portable package

First Linux target: Ubuntu 22.04 / 24.04, x86_64, GNOME on X11 or Wayland.
Other distributions, desktops and architectures are not validated. A real desktop
session and a default browser are required. No Python, Node.js, Rust or compiler
installation is required to run the package.

1. Download the `ilab-gpt-conjure_linux_portable_x64_VERSION.tar.gz` asset and its
   `.sha256.txt` sidecar from the same release. Run `sha256sum -c FILE.sha256.txt`.
2. Install desktop runtime libraries if missing:
   - Ubuntu 22.04: `sudo apt install libgtk-3-0 libayatana-appindicator3-1 libxdo3 xdg-utils curl ca-certificates zenity libglib2.0-bin`
   - Ubuntu 24.04: `sudo apt install libgtk-3-0t64 libayatana-appindicator3-1 libxdo3 xdg-utils curl ca-certificates zenity libglib2.0-bin`
3. Extract the whole archive into a directory you own and can write to. Do not run
   from inside the archive, `/opt`, or a read-only/network mount.
4. Run `./Start-WebUI.sh` in the extracted directory. If your file manager does not
   execute scripts, run this command in a terminal. Do not use `sudo`.

The native launcher opens the local WebUI, logs diagnostics in `data/webui-outputs/webui-server.log` by default,
and provides tray actions. Ubuntu's AppIndicator GNOME extension must be enabled
for the tray icon; library presence alone does not guarantee tray availability.
Keep the terminal error text if the display or tray is unavailable.

All mutable settings, credentials, task database, default inputs, outputs,
thumbnails, gallery, backups and logs live under `data/`. Explicitly configured
external storage paths are retained. `ILAB_CONJURE_DATA_DIR` can override the data
root, but normal operation does not require environment variables.

## Upgrade safely

Linux v1 checks signed update metadata and offers downloads. It never automatically
replaces program files and does not execute the macOS `.command` updater.

1. Quit the old launcher through its tray menu and wait for its backend to stop.
2. Back up the complete `data/` directory and any separately configured storage.
3. Download, verify and extract the new complete package into a new directory.
4. Copy the old `data/` directory into the new directory, keeping the old directory
   as a recovery copy. Do not overwrite it with the empty data directory in a new
   archive. Keep explicit external input/output directories where they are.
5. Start the new launcher and check settings, history and a saved image before
   removing old program files. Never delete your only data or backup copy.

Moving a portable package does not rewrite explicitly configured absolute paths.
Review custom input/output/gallery paths before retiring the previous directory.

For installation, build instructions and the pending real-session qualification
matrix, see https://github.com/Tran314/ilab-conjure/blob/main/docs/linux.md.
