"""Paths and constants. Everything is overridable by environment so tests never touch real data."""
import os
import re
import uuid
from pathlib import Path

def _env(name: str):
    """EVENFALL_<name>, falling back to the old ENGRAM_<name> for one release."""
    return os.environ.get("EVENFALL_" + name) or os.environ.get("ENGRAM_" + name)


HOME = Path(_env("HOME") or Path.home() / ".evenfall")
SRC = Path(_env("SRC") or Path.home() / ".claude" / "projects")

def project_key(path: str) -> str:
    """How Claude Code names a project folder: every character that is not a letter or digit becomes '-'.
    /Users/jo/app -> -Users-jo-app, C:\\Users\\jo\\app -> C--Users-jo-app."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def path_name(path: str) -> str:
    """Last component of a path written with / or \\ (transcripts hold whichever the tool used)."""
    return re.split(r"[\\/]+", path.rstrip("\\/"))[-1]


HOME_KEY = project_key(str(Path.home()))      # a project started in the home folder, e.g. -Users-jo or C--Users-jo
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
        return p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        STATE.mkdir(parents=True, exist_ok=True)
        mid = uuid.uuid4().hex[:12]
        p.write_text(mid + "\n", encoding="utf-8")
        return mid
