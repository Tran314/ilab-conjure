# Linux installed DEB

First target: Ubuntu 22.04 / 24.04 x86_64, GNOME, X11 or Wayland. Download
`iLab-GPT-CONJURE-linux-x64_VERSION.deb` and its `.sha256.txt` sidecar, then run:

```sh
sha256sum -c iLab-GPT-CONJURE-linux-x64_VERSION.deb.sha256.txt
sudo apt install ./iLab-GPT-CONJURE-linux-x64_VERSION.deb
ilab-conjure
```

You can also open iLab CONJURE from the desktop application menu. Use `apt install`
so GTK3 and AppIndicator runtime dependencies are resolved for your Ubuntu version
(including the Ubuntu 24.04 `t64` GTK package). No user Python, Node.js or Rust is
needed. Run as your normal desktop user, never as root. Enable the Ubuntu
AppIndicator extension if GNOME does not display the tray.

Program files are root-owned under `/opt/ilab-conjure` and should be treated as
read-only. User data is separate, under `$XDG_DATA_HOME/iLab GPT CONJURE` when
`XDG_DATA_HOME` is a nonempty absolute path, otherwise
`~/.local/share/iLab GPT CONJURE`. Relative XDG paths are ignored. Custom input,
output, gallery and source-data paths in settings are preserved. Program updates
must never write a fresh empty data tree over this directory.

## Upgrade, remove or switch packages

Quit iLab CONJURE before an upgrade. Back up the data directory and separately
configured storage, verify the new DEB's checksum, and run `sudo apt install
./NEW.deb`. The package contains no user data and no data-removing maintainer
scripts. `sudo apt remove ilab-conjure` removes program files only; your user data
remains. Linux v1 offers signed update checks/downloads, with no automatic
replacement, privilege elevation or package-manager invocation from the app.

When switching from portable, quit both launchers, back up both data directories,
and copy the portable `data/` contents into the installed data directory only if
you intend to use that profile. Do not overwrite a populated profile without a
backup. Explicit external storage paths remain unchanged; verify them in Settings.

The eight Ubuntu × display × package real-session checks are recorded separately
in `docs/linux.md`; headless CI is not proof of desktop compatibility.
