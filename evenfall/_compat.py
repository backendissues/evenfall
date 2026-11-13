"""The few places where Windows and POSIX differ: file locks, durable writes, binary file descriptors, detached child processes.
Everything else in evenfall is portable standard-library Python."""
import os
import subprocess
import sys
import time

IS_WINDOWS = sys.platform == "win32"
O_BINARY = getattr(os, "O_BINARY", 0)      # Windows translates \n in fds unless told not to; POSIX has no such flag

if IS_WINDOWS:
    import msvcrt

    def try_lock(fd: int) -> bool:
        """Take an exclusive lock on the file without waiting. False means someone else holds it."""
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def lock(fd: int, poll: float = 0.05) -> None:
    """Take an exclusive lock, waiting as long as it takes."""
    if not IS_WINDOWS:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    while not try_lock(fd):
        time.sleep(poll)


def full_sync(fd: int) -> None:
    """Flush a file to the physical disk. macOS needs F_FULLFSYNC (os.fsync alone stops at the drive cache)."""
    if not IS_WINDOWS and hasattr(fcntl, "F_FULLFSYNC"):
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)


def sync_dir(path: str) -> None:
    """Make a new directory entry durable. Windows cannot open a directory, and its file system does not need this."""
    if IS_WINDOWS:
        return
    dfd = os.open(path, os.O_RDONLY)
    try:
        full_sync(dfd)
    finally:
        os.close(dfd)


def replace(src, dst, tries: int = 20) -> None:
    """os.replace that waits out Windows' transient "file in use" errors (another process reading the target for a moment)."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not IS_WINDOWS or i == tries - 1:
                raise
            time.sleep(0.05)


def python_exe(background: bool = False) -> str:
    """The interpreter to launch. For unattended runs on Windows prefer pythonw.exe so no console window flashes."""
    exe = sys.executable
    if IS_WINDOWS and background:
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            return w
    return exe


def spawn_detached(args: list) -> None:
    """Start a child that outlives this process, with no console and no inherited handles."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if IS_WINDOWS:
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)
