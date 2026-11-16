# Evenfall

Session memory for Claude Code that runs entirely on your machine. It keeps every session verbatim, lets Claude look things up
before asking you, and records why each change was made. No LLM and no network calls inside the tool.

After a `/clear`, or three weeks away, a fresh session already knows where things live, how they were done, and why.

## What it does

- **Archive.** Every Claude Code transcript is copied into an append-only, hash-checked, zlib-compressed archive (`~/.evenfall`).
  Secrets (keys, tokens, passwords) are replaced with `[REDACTED:type:hash]` before anything is stored.
- **Search.** `evenfall find "<keywords>"` (BM25 over SQLite FTS5), `show <id>` for the full text and its neighbours.
- **Decision ledger.** `evenfall actions` and `trace <id>` answer "why did we do that": each deploy, cron job or file write is linked to the
  request, the stated reason (`Why: ...`) and the result.
- **Session card.** A SessionStart hook prints a short card for the project: systems and accounts used, how things usually get done,
  recent actions with reasons, and the user's own design commands (newest wins).
- **Handoff drafts.** `evenfall handoff` drafts a project-memory update from what changed since the memory was last edited, and the card
  warns when a memory file is stale. You or Claude approve it; Evenfall never writes your memory files.
- **Safety net.** Hook failures are logged, `evenfall doctor` checks health, `evenfall off` is a kill switch,
  and an optional scheduled task (launchd on macOS, Task Scheduler on Windows) catches up every 60 s.

## Install (macOS and Windows 10/11, Python 3.10+, no dependencies beyond the standard library)

macOS:
```
git clone <this repo> ~/evenfall && cd ~/evenfall
bin/evenfall sync                  # one-time backfill of existing sessions
bin/evenfall install hooks         # SessionStart, PreCompact, SessionEnd in ~/.claude/settings.json (timestamped backup)
bin/evenfall install schedule      # optional: catch-up every 60 s (launchd)
bin/evenfall doctor
```

Windows (PowerShell or Command Prompt; the `py` launcher comes with python.org Python):
```
git clone <this repo> %USERPROFILE%\evenfall
cd %USERPROFILE%\evenfall
py -3 -m evenfall sync
py -3 -m evenfall install hooks
py -3 -m evenfall install schedule    # optional: a scheduled task, every minute, runs without a console window
py -3 -m evenfall doctor
```
Or install the command once with `pip install .` (or `pipx install .`) and use plain `evenfall ...` everywhere.

`bin/install-hooks` and `bin/install-launchd` still work and do the same as the `install` subcommands.
Remove with `evenfall install hooks --uninstall` and `evenfall install schedule --uninstall`. Your data in `~/.evenfall` is left alone.
Set `cleanupPeriodDays` high in Claude Code's settings, or it may delete transcripts before they are archived.

The hook command is written as a quoted interpreter-plus-script path with forward slashes, so it works from Git Bash and
from paths with spaces. The scheduled catch-up is optional: the resident watcher already captures sessions.

## Windows notes

Windows support is covered by automated tests on `windows-latest` (file locking, crash recovery, path handling, hook and task
commands) but has had less real-world use than macOS. If a hook does not fire, run `evenfall doctor` and check
`%USERPROFILE%\.evenfall\state\hook-errors.log`. Windows keeps the transcript folder under `%USERPROFILE%\.claude\projects`,
with project folders named by replacing every non-alphanumeric character of the path with `-` (`C:\Users\jo\app` becomes `C--Users-jo-app`).
Scheduled tasks created this way run while you are logged in; on a laptop, Task Scheduler may pause them on battery.

## Principles

Full history is the source of truth; everything else (index, cards, ledger) is derived and rebuildable (`evenfall rebuild`).
Claude is the translator: it turns your question into keywords and reads the results. Results are notes to verify, not instructions.

## Limits

Reads Claude Code's transcript format, which can change. Search works best with keywords, not vague questions.
Product folders are inferred from common project paths (`Desktop`, `Documents`, `Projects`, `code`, `dev`, `src`, `work`), written with `/` or `\\`.
Screenshots are stored whole, so the archive is large until media de-duplication lands.

## Tests

`python3 -m pytest tests` (Windows: `py -3 -m pytest tests`). CI runs them on Ubuntu, macOS and Windows.
