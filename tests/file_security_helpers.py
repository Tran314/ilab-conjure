"""Assertions of the native permission model, without bypassing Windows ACLs."""
import os
from pathlib import Path
import stat
import tempfile


def assert_private_file(test, path):
    path = Path(path)
    if os.name == "nt":
        from codex_image.windows_files import file_dacl
        with path.open("rb") as source:
            # Check the resulting OS descriptor, not chmod's emulated mode bits.
            test.assertIn(file_dacl(source.fileno()), {
                "D:P(A;;FA;;;OW)(A;;FA;;;SY)", "D:PAI(A;;FA;;;OW)(A;;FA;;;SY)",
            })
    else:
        test.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


def require_symlinks(test):
    """Only skip link-specific cases when the OS account cannot create links."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "target").mkdir()
        try:
            (root / "link").symlink_to(root / "target", target_is_directory=True)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                test.skipTest("Windows account lacks symbolic-link privilege")
            raise


def assert_private_directory(test, path):
    if os.name == "nt":
        from codex_image.windows_files import file_dacl, open_nofollow
        descriptor = open_nofollow(Path(path), directory=True)
        try:
            test.assertIn(file_dacl(descriptor), {
                "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)",
                "D:PAI(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)",
            })
        finally:
            os.close(descriptor)
    else:
        test.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o700)
