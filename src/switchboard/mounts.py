"""Darwin's read-only mount inventory; no subprocess or arbitrary path discovery."""

import ctypes
import sys
import threading

_LOCK = threading.Lock()


class StatFS(ctypes.Structure):
    # Darwin <sys/mount.h> __DARWIN_STRUCT_STATFS64 (macOS 64-bit ABI).
    _fields_ = [
        ("bsize", ctypes.c_uint32),
        ("iosize", ctypes.c_int32),
        *[(key, ctypes.c_uint64) for key in ("blocks", "bfree", "bavail", "files", "ffree")],
        ("fsid", ctypes.c_int32 * 2),
        ("owner", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("subtype", ctypes.c_uint32),
        ("fs", ctypes.c_char * 16),
        ("mount", ctypes.c_char * 1024),
        ("device", ctypes.c_char * 1024),
        ("flags_ext", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 7),
    ]


def mounted_volumes():
    # libc owns a static buffer; serialize the call and copy across Core instances.
    with _LOCK:
        return _mounted_volumes()


def _mounted_volumes():
    if sys.platform != "darwin":
        raise OSError("Native mount discovery requires macOS")
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    # Intel exports the INODE64 alias; Apple Silicon uses the 64-bit ABI only.
    getmntinfo = getattr(libc, "getmntinfo$INODE64", None) or libc.getmntinfo
    getmntinfo.argtypes = [ctypes.POINTER(ctypes.POINTER(StatFS)), ctypes.c_int]
    getmntinfo.restype = ctypes.c_int
    buffer = ctypes.POINTER(StatFS)()
    count = getmntinfo(ctypes.byref(buffer), 2)  # MNT_NOWAIT: don't refresh/wake mounts.
    if count <= 0 or count > 4096:
        raise OSError("Mount inventory unavailable")
    return [
        {
            "device": bytes(row.device).decode("utf-8", "replace"),
            "mount": bytes(row.mount).decode("utf-8", "replace"),
            "fs": bytes(row.fs).decode("ascii", "replace"),
            "readonly": bool(row.flags & 1),
        }
        for row in buffer[:count]
    ]
