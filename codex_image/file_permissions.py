"""Private permissions on an already-created, unpublished file."""
import os
import stat
from pathlib import Path


def restrict_file_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        from .windows_files import restrict_file_descriptor as restrict_windows
        restrict_windows(descriptor)
    else:
        os.fchmod(descriptor, 0o600)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            raise OSError("private_file_mode_failed")


def is_private_file_descriptor(descriptor: int) -> bool:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        return False
    if os.name == "nt":
        from .windows_files import is_private_file_descriptor as is_private_windows
        return is_private_windows(descriptor)
    return not (os.fstat(descriptor).st_mode & 0o077)


def restrict_directory(path: Path) -> None:
    if os.name == "nt":
        from .windows_files import open_nofollow
        descriptor = open_nofollow(path, directory=True, write_dacl=True)
        try:
            restrict_file_descriptor(descriptor)
        finally:
            os.close(descriptor)
    else:
        os.chmod(path, 0o700)
