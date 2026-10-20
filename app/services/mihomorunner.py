"""Exec the Linux core with a parent-death signal, including abrupt API exits."""
import ctypes
import os
import signal
import sys


if __name__ == "__main__":
    expected_parent = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot configure Mihomo parent-death signal")
    if os.getppid() != expected_parent:
        sys.exit(0)
    os.execv(sys.argv[2], sys.argv[2:])
