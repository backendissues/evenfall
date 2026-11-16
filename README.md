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
  and an optional launchd agent catches up every 60 s.

## Install (macOS, Python 3.10+, no dependencies beyond the standard library)

```
git clone <this repo> ~/evenfall && cd ~/evenfall
bin/evenfall sync            # one-time backfill of existing sessions
bin/install-hooks            # SessionStart, PreCompact, SessionEnd in ~/.claude/settings.json (timestamped backup)
bin/install-launchd          # optional: catch-up every 60 s
bin/evenfall doctor
```

Remove with `bin/install-hooks --uninstall` and `bin/install-launchd --uninstall`. Your data in `~/.evenfall` is left alone.
Set `cleanupPeriodDays` high in Claude Code's settings, or it may delete transcripts before they are archived.

## Principles

Full history is the source of truth; everything else (index, cards, ledger) is derived and rebuildable (`evenfall rebuild`).
Claude is the translator: it turns your question into keywords and reads the results. Results are notes to verify, not instructions.

## Limits

Reads Claude Code's transcript format, which can change. Search works best with keywords, not vague questions.
Product folders are inferred from common project paths (`Desktop`, `Documents`, `Projects`, `code`, `dev`, `src`, `work`).
Screenshots are stored whole, so the archive is large until media de-duplication lands.

## Tests

`python3 -m pytest tests`
