"""Paths and constants. Everything is overridable by environment so tests never touch real data."""
import os
import uuid
from pathlib import Path

def _env(name: str):
    """EVENFALL_<name>, falling back to the old ENGRAM_<name> for one release."""
    return os.environ.get("EVENFALL_" + name) or os.environ.get("ENGRAM_" + name)


HOME = Path(_env("HOME") or Path.home() / ".evenfall")
SRC = Path(_env("SRC") or Path.home() / ".claude" / "projects")

HOME_KEY = str(Path.home()).replace("/", "-")      # how Claude Code names a project started in the home folder, e.g. -Users-jo
MEMORY_DIR = Path(_env("MEMORY") or Path.home() / ".claude" / "projects" / HOME_KEY / "memory")
ARCHIVE = HOME / "archive"
STATE = HOME / "state"
DB_PATH = HOME / "evenfall.db"

READ_BLOCK = 8 * 1024 * 1024      # bytes read from a transcript per step
MAX_OPEN_SEGMENTS = 8


def machine_id() -> str:
    """Stable per-machine id. Each machine only ever writes under archive/<machine_id>/."""
    p = STATE / "machine_id"
    try:
        return p.read_text().strip()
    except FileNotFoundError:
        STATE.mkdir(parents=True, exist_ok=True)
        mid = uuid.uuid4().hex[:12]
        p.write_text(mid + "\n")
        return mid
