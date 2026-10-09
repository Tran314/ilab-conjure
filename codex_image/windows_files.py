"""Handle-based Windows equivalents of private modes and no-follow file access.

Imported only on Windows. Directory handles deny delete sharing, so callers can
pin every path component while enumerating or opening children by path. Reparse
points are rejected, including junctions. Deletion targets the verified handle.
"""
from __future__ import annotations

from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import msvcrt
import os
from pathlib import Path
import stat

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi = ctypes.WinDLL("advapi32", use_last_error=True)


def _function(library, name, result, *arguments):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = arguments
    return function


_create = _function(_kernel, "CreateFileW", wintypes.HANDLE, wintypes.LPCWSTR,
                    wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
_close = _function(_kernel, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_reopen = _function(_kernel, "ReOpenFile", wintypes.HANDLE, wintypes.HANDLE,
                   wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
_free = _function(_kernel, "LocalFree", wintypes.HANDLE, wintypes.HANDLE)
_file_type = _function(_kernel, "GetFileType", wintypes.DWORD, wintypes.HANDLE)
_set_info = _function(_kernel, "SetFileInformationByHandle", wintypes.BOOL,
                     wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
_convert_sd = _function(_advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                       wintypes.BOOL, wintypes.LPCWSTR, wintypes.DWORD,
                       ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD))
_get_dacl = _function(_advapi, "GetSecurityDescriptorDacl", wintypes.BOOL,
                     wintypes.LPVOID, ctypes.POINTER(wintypes.BOOL),
                     ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.BOOL))
_set_security = _function(_advapi, "SetSecurityInfo", wintypes.DWORD,
                         wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                         wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID)
_get_security = _function(_advapi, "GetSecurityInfo", wintypes.DWORD,
                         wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                         wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID,
                         ctypes.POINTER(wintypes.LPVOID))
_sd_string = _function(_advapi, "ConvertSecurityDescriptorToStringSecurityDescriptorW",
                      wintypes.BOOL, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                      ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD))


class _FileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation", wintypes.FILETIME), ("access", wintypes.FILETIME),
        ("write", wintypes.FILETIME), ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD), ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


_get_info = _function(_kernel, "GetFileInformationByHandle", wintypes.BOOL,
                     wintypes.HANDLE, ctypes.POINTER(_FileInformation))


def _checked_handle(handle):
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def restrict_file_descriptor(descriptor: int) -> None:
    """Protect the existing file's DACL: only its owner and SYSTEM have access."""
    directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
    inheritance = "OICI" if directory else ""
    security = wintypes.LPVOID()
    sddl = f"D:P(A;{inheritance};FA;;;OW)(A;{inheritance};FA;;;SY)"
    if not _convert_sd(sddl, 1, ctypes.byref(security), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), wintypes.LPVOID()
        if not _get_dacl(security, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        # CRT file handles do not request WRITE_DAC. Reopen the same object,
        # rather than resolving its path again, to avoid a replacement race.
        original = msvcrt.get_osfhandle(descriptor)
        handle = original if directory else _checked_handle(_reopen(original, 0x60000, 7, 0))
        try:
            error = _set_security(handle, 1, 0x80000004, None, None, acl, None)
            if error:
                raise ctypes.WinError(error)
        finally:
            if not directory:
                _close(handle)
    finally:
        _free(security)


def open_nofollow(
    path: Path, *, directory: bool = False, delete: bool = False, write_dacl: bool = False,
) -> int:
    """Open a disk object, reject reparse points, and pin its identity."""
    access = 0x80000000 | (0x10000 if delete else 0)  # GENERIC_READ, DELETE
    if write_dacl:
        access |= 0x40000  # WRITE_DAC; GENERIC_READ already includes READ_CONTROL.
    share = 1  # FILE_SHARE_READ only: deny replacement and reparse-point writers.
    handle = _checked_handle(_create(str(path), access, share, None, 3,
                                     0x00200000 | 0x02000000, None))
    try:
        info = _FileInformation()
        if not _get_info(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (info.attributes & 0x400 or bool(info.attributes & 0x10) != directory
                or _file_type(handle) != 1):
            raise OSError("backup_restore_path_invalid")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None  # CRT owns the handle now.
        return descriptor
    finally:
        if handle is not None:
            _close(handle)


def file_dacl(descriptor: int) -> str:
    """Read the actual DACL from the opened object, including protection flags."""
    security, text = wintypes.LPVOID(), wintypes.LPWSTR()
    error = _get_security(msvcrt.get_osfhandle(descriptor), 1, 4,
                          None, None, None, None, ctypes.byref(security))
    if error:
        raise ctypes.WinError(error)
    try:
        if not _sd_string(security, 1, 4, ctypes.byref(text), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return text.value
    finally:
        if text:
            _free(text)
        _free(security)


def is_private_file_descriptor(descriptor: int) -> bool:
    value = file_dacl(descriptor)
    flags, _, entries = value.partition("(")
    return (flags in {"D:P", "D:PAI"}
            and sorted(("(" + entries).split(")")[:-1])
            == sorted(["(A;;FA;;;OW", "(A;;FA;;;SY"]))


def unlink_nofollow(root: Path, path: Path) -> None:
    """Pin all parents and delete the opened regular file, never a new target."""
    relative = path.relative_to(root)
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise OSError("backup_restore_path_invalid")
    with ExitStack() as stack:
        cursor = root
        for part in (None, *relative.parts[:-1]):
            if part is not None:
                cursor /= part
            descriptor = open_nofollow(cursor, directory=True)
            stack.callback(os.close, descriptor)
        try:
            descriptor = open_nofollow(path, delete=True)
        except FileNotFoundError:
            return
        stack.callback(os.close, descriptor)
        # FILE_DISPOSITION_INFO uses a one-byte BOOLEAN, not Win32 BOOL.
        disposition = ctypes.c_ubyte(1)
        if not _set_info(msvcrt.get_osfhandle(descriptor), 4,
                         ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise ctypes.WinError(ctypes.get_last_error())
