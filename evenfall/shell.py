"""Tiny shell-line helpers shared by the ledger and the project card."""
import re
import shlex

KEYWORDS = {"for", "do", "done", "if", "then", "else", "elif", "fi", "while", "until", "case", "esac", "in", "function", "time", "!", "{", "}", "[[", "]]"}
COMMAND_FOLLOWS = {"do", "then", "else", "elif", "if", "while", "until", "time", "!", "{"}   # the next word is a command again
CMD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._+-]*$")
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\s*(?:\n|$)", re.S)
REMOTE = re.compile(r"\b(git\s+push|git\s+pull|git\s+fetch|gh\s+\w+|rsync|scp|sftp|ssh|curl|launchctl|crontab|npm\s+publish|vercel|kubectl|docker|claude\s+-p)\b")


def strip_heredocs(cmd: str) -> str:
    return HEREDOC.sub("\n", cmd)


def command_names(cmd: str) -> list:
    """First word of each simple command, respecting quotes and skipping heredoc bodies, so code is not mistaken for commands."""
    cmd = strip_heredocs(cmd.replace("\\n", "\n")).replace("\n", " ; ")      # a newline separates commands, quotes protect their own text
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        toks = cmd.split()[:1]
    names, fresh = [], True
    for t in toks:
        if t and set(t) <= set(";&|"):
            fresh = True
            continue
        if fresh:
            fresh = False
            base = re.split(r"[\\/]", t)[-1]
            if "=" in t or base in KEYWORDS or not CMD_NAME.match(base):
                fresh = "=" in t or base in COMMAND_FOLLOWS
                continue
            names.append(base)
    return names


LOCAL_URL = re.compile(r"(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)")
ANY_URL = re.compile(r"https?://(?!localhost|127\.|\[::1\]|0\.0\.0\.0)")


def remote_op(cmd: str):
    """Name of the outward-facing operation in a command (git push, rsync, ssh, curl...), or None for local-only work."""
    c = re.sub(r"\bgit\s+-C\s+\S+", "git", strip_heredocs(cmd))
    m = REMOTE.search(c)
    if not m:
        return None
    op = " ".join(m.group(1).split()[:2])
    if op == "curl" and LOCAL_URL.search(c) and not ANY_URL.search(c):
        return None                                   # curl to a local dev server is testing, not an outward action
    return op
