"""Install or remove the Claude Code hooks and the optional scheduled catch-up, on macOS (launchd) and Windows (Task Scheduler)."""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import _compat, brief, config

EVENTS = {"SessionStart": ("start", 10), "PreCompact": ("sync", 5), "SessionEnd": ("end", 5)}
LABEL = "com.evenfall.sync"                                    # launchd
PLIST = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
TASK = "evenfall-sync"                                         # Task Scheduler


def default_settings() -> Path:
    return Path(config._env("SETTINGS") or Path.home() / ".claude" / "settings.json")


def install_hooks(settings=None, uninstall: bool = False) -> str:
    """Add (or remove) the three hooks. Idempotent: any earlier evenfall entry, in any install form, is replaced. Keeps a timestamped backup."""
    settings = Path(settings or default_settings())
    if settings.exists():
        d = json.loads(settings.read_text(encoding="utf-8"))
        shutil.copy2(settings, "%s.bak-%s" % (settings, time.strftime("%Y%m%d-%H%M%S")))
    else:
        d = {}
        settings.parent.mkdir(parents=True, exist_ok=True)
    hooks = d.setdefault("hooks", {})
    for ev, (arg, timeout) in EVENTS.items():
        kept = [g for g in hooks.get(ev, []) if not any(brief.is_hook_command(h.get("command", "")) for h in g.get("hooks", []))]
        if not uninstall:
            kept.append({"hooks": [{"type": "command", "command": brief.shell_command("hook", arg), "timeout": timeout}]})
        if kept:
            hooks[ev] = kept
        else:
            hooks.pop(ev, None)
    if not hooks:
        d.pop("hooks")
    settings.write_text(json.dumps(d, indent=2), encoding="utf-8", newline="\n")
    json.loads(settings.read_text(encoding="utf-8"))            # must still parse
    return ("removed from " if uninstall else "installed in ") + str(settings)


def _task_run() -> str:
    """The command line Task Scheduler runs (the /TR value): the interpreter without a console window, the launcher, the sync."""
    py, script = _compat.python_exe(background=True), brief.BIN
    return '"%s" "%s" sync --scheduled' % (py, script)


def schtasks_args(uninstall: bool = False) -> list:
    if uninstall:
        return ["schtasks", "/Delete", "/TN", TASK, "/F"]
    return ["schtasks", "/Create", "/SC", "MINUTE", "/MO", "1", "/TN", TASK, "/TR", _task_run(), "/F"]


def launchd_plist() -> dict:
    log = str(config.STATE / "launchd.log")
    return {"Label": LABEL, "ProgramArguments": brief.command("sync", "--scheduled"), "StartInterval": 60,
            "StandardOutPath": log, "StandardErrorPath": log, "ProcessType": "Background", "Nice": 10, "RunAtLoad": True}


def install_schedule(uninstall: bool = False, dry_run: bool = False) -> int:
    if sys.platform == "darwin":
        uid = str(os.getuid())
        if uninstall:
            subprocess.run(["launchctl", "bootout", "gui/" + uid, str(PLIST)], capture_output=True)
            if PLIST.exists():
                PLIST.unlink()
            print("removed", PLIST)
            return 0
        if dry_run:
            print(plistlib.dumps(launchd_plist()).decode())
            return 0
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        config.STATE.mkdir(parents=True, exist_ok=True)
        with open(PLIST, "wb") as f:
            plistlib.dump(launchd_plist(), f)
        subprocess.run(["launchctl", "bootout", "gui/" + uid, str(PLIST)], capture_output=True)   # reload cleanly if already loaded
        r = subprocess.run(["launchctl", "bootstrap", "gui/" + uid, str(PLIST)], capture_output=True, text=True)
        print("installed", PLIST, "| launchctl:", (r.stderr or "ok").strip())
        return r.returncode
    if _compat.IS_WINDOWS:
        args = schtasks_args(uninstall)
        if dry_run:
            print(subprocess.list2cmdline(args))
            return 0
        r = subprocess.run(args, capture_output=True, text=True)
        print(("removed" if uninstall else "installed") + " scheduled task %s | schtasks: %s" % (TASK, (r.stdout + r.stderr).strip() or "ok"))
        return r.returncode
    print("No scheduler support on this platform. That is fine: the resident watcher already captures sessions (see `evenfall doctor`).")
    return 0


def schedule_loaded() -> bool:
    try:
        if sys.platform == "darwin":
            return LABEL in subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
        if _compat.IS_WINDOWS:
            return subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True, timeout=10).returncode == 0
    except Exception:
        pass
    return False
