"""Native, inode-preserving publication. No overwrite fallback is safe."""
from __future__ import annotations

import ctypes
import errno
import os
import sys


def rename(old_fd: int, old: str, new_fd: int, new: str, *, exchange: bool, after=None) -> None:
    """Exchange existing names, or rename without replacing a destination.

    Open editor descriptors continue referring to the displaced inode. Callers
    must retain that inode and fsync both directories after publication.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = libc.renameatx_np
        flag = 2 if exchange else 4  # RENAME_SWAP / RENAME_EXCL
    elif sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        if function is None:
            raise OSError(errno.ENOTSUP, "native no-clobber/exchange unavailable")
        flag = 2 if exchange else 1  # RENAME_EXCHANGE / RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "native no-clobber/exchange unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(old_fd, os.fsencode(old), new_fd, os.fsencode(new), flag):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    if after is not None:
        after()
    os.fsync(old_fd)
    os.fsync(new_fd)
