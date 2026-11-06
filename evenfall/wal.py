"""Append-only, crash-safe segment files.

Layout of a segment:  MAGIC, then records of  [u32 length][u32 crc32][zlib(json)].
A torn tail (power cut mid-write) is detected by length/CRC and cut off on the next open.
Durability: commit() flushes with F_FULLFSYNC on macOS (os.fsync alone does not reach the disk there).
"""
import fcntl
import json
import os
import struct
import zlib

MAGIC = b"ENGRAMW1"   # file-format marker: kept from the tool's first name so existing archives stay readable
HDR = struct.Struct(">II")
MAX_RECORD = 64 * 1024 * 1024


def _full_sync(fd: int) -> None:
    if hasattr(fcntl, "F_FULLFSYNC"):
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)


def encode(rec: dict) -> bytes:
    body = zlib.compress(json.dumps(rec, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass"), 6)
    return HDR.pack(len(body), zlib.crc32(body)) + body


def scan(path, start=0):
    """Yield (record, end_offset) for every valid record; stop silently at the first bad one.
    `start` must be a record boundary previously returned as an end_offset."""
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            return
        pos = len(MAGIC)
        if start > pos:
            f.seek(start)
            pos = start
        while True:
            h = f.read(HDR.size)
            if len(h) < HDR.size:
                return
            n, crc = HDR.unpack(h)
            if n > MAX_RECORD:
                return
            body = f.read(n)
            if len(body) < n or zlib.crc32(body) != crc:
                return
            try:
                rec = json.loads(zlib.decompress(body).decode("utf-8", "surrogatepass"))
            except Exception:
                return
            pos += HDR.size + n
            yield rec, pos


def valid_length(path) -> int:
    """Byte length of the valid prefix of a segment (0 if the header itself is missing)."""
    end = len(MAGIC)
    try:
        with open(path, "rb") as f:
            if f.read(len(MAGIC)) != MAGIC:
                return 0
    except FileNotFoundError:
        return 0
    for _rec, pos in scan(path):
        end = pos
    return end


class Segment:
    """One open segment. append() buffers; commit() writes and flushes to disk."""

    def __init__(self, path):
        self.path = str(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        new = not os.path.exists(self.path)
        if not new:
            good = valid_length(self.path)
            if good == 0:
                os.truncate(self.path, 0)
                new = True
            elif good < os.path.getsize(self.path):
                os.truncate(self.path, good)          # repair a torn tail
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.buf = []
        if new:
            os.write(self.fd, MAGIC)
            self._new = True
        else:
            self._new = False

    def append(self, rec: dict) -> None:
        self.buf.append(encode(rec))

    @property
    def dirty(self) -> bool:
        return bool(self.buf) or self._new

    def write_out(self) -> None:
        if self.buf:
            os.write(self.fd, b"".join(self.buf))
            self.buf = []

    def sync(self) -> None:
        _full_sync(self.fd)
        if self._new:                                 # make the new directory entry durable too
            dfd = os.open(os.path.dirname(self.path), os.O_RDONLY)
            try:
                _full_sync(dfd)
            finally:
                os.close(dfd)
            self._new = False

    def close(self) -> None:
        self.write_out()
        os.close(self.fd)
