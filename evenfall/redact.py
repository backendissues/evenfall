"""Secret scrubber, applied before anything is written to the archive or index.

A secret becomes [REDACTED:<kind>:<8 hex of its blake2b>], so the record still shows that something was there,
and the same secret always maps to the same token. Pure hex strings are never touched: git SHAs and content
hashes are identifiers, not secrets.
"""
import collections
import hashlib
import json
import math
import re

_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_KEY = re.compile(
    r"\b(?:sk-ant-[A-Za-z0-9_\-]{20,}|sk-[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"
    r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{35}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})")
_AUTH = re.compile(r"(?i)\b(bearer|basic)(\s+)([A-Za-z0-9._~+/=\-]{16,})")
_ASSIGN = re.compile(
    r"(?i)(\b[A-Za-z0-9_.\-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|credential)[A-Za-z0-9_.\-]*)"
    r"((?:\\?[\"'])?\s*[:=]\s*)((?:\\?[\"'])?)([^\s\"'\\,;}{\]\)]{6,})")
# long mixed-case-with-digits blobs (base64-ish keys); pure hex has no uppercase+lowercase mix so it is skipped
_BLOB = re.compile(
    r"(?<![A-Za-z0-9+/_\-])(?=[A-Za-z0-9+/_\-]{40,}(?![A-Za-z0-9+/_\-]))(?=[A-Za-z0-9+/_\-]*[A-Z])"
    r"(?=[A-Za-z0-9+/_\-]*[a-z])(?=[A-Za-z0-9+/_\-]*\d)[A-Za-z0-9+/_\-]{40,}")


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in collections.Counter(s).values())


def _looks_secret(s: str) -> bool:
    """High-entropy and not shaped like a path or identifier (paths, tool names and slugs are not secrets)."""
    if s.startswith("/") or s.count("/") >= 3 or re.match(r"[A-Za-z]:[\\/]", s) or s.count("\\") >= 2 or "__" in s or s.count("-") + s.count("_") >= 4:
        return False
    return _entropy(s) >= 4.5


def _tok(kind: str, s: str) -> str:
    return "[REDACTED:%s:%s]" % (kind, hashlib.blake2b(s.encode("utf-8", "replace"), digest_size=4).hexdigest())


def redact(text: str, counts: dict = None) -> str:
    def bump(kind):
        if counts is not None:
            counts[kind] = counts.get(kind, 0) + 1

    def sub_simple(kind):
        def f(m):
            bump(kind)
            return _tok(kind, m.group(0))
        return f

    def sub_auth(m):
        bump("auth")
        return m.group(1) + m.group(2) + _tok("auth", m.group(3))

    def sub_assign(m):
        val = m.group(4)
        if (val.startswith("[REDACTED") or val.isdigit() or val.startswith(("/", "~", "http", "$", "<"))
                or not any(c.isdigit() for c in val)):
            return m.group(0)
        bump("secret")
        return m.group(1) + m.group(2) + m.group(3) + _tok("secret", val)

    def sub_blob(m):
        s = m.group(0)
        pre = ""
        run, k = 0, m.start() - 1
        while k >= 0 and m.string[k] == "\\":
            run, k = run + 1, k - 1
        if run % 2 == 1:                       # an odd run means the first char is a JSON escape letter (\n, \t, \uXXXX)
            cut = 5 if s[0] == "u" else 1
            pre, s = s[:cut], s[cut:]
        if len(s) < 40 or not _looks_secret(s):
            return m.group(0)
        bump("blob")
        return pre + _tok("blob", s)

    if "-----BEGIN" in text:
        text = _PEM.sub(sub_simple("pem"), text)
    text = _KEY.sub(sub_simple("key"), text)
    text = _AUTH.sub(sub_auth, text)
    text = _ASSIGN.sub(sub_assign, text)
    text = _BLOB.sub(sub_blob, text)
    return text


def _walk(o, counts):
    if isinstance(o, str):
        return redact(o, counts)
    if isinstance(o, list):
        return [_walk(x, counts) for x in o]
    if isinstance(o, dict):
        return {k: _walk(v, counts) for k, v in o.items()}
    return o


def redact_line(raw: str, counts: dict = None) -> str:
    """Redact one JSONL line, guaranteeing the result is still valid JSON whenever the input was."""
    out = redact(raw, counts)
    if out == raw:
        return out
    try:
        json.loads(out)
        return out
    except ValueError:
        pass
    try:
        d = json.loads(raw)
    except ValueError:
        return out                                   # the source line was not JSON to begin with
    if counts is not None:
        counts["json_fallback"] = counts.get("json_fallback", 0) + 1
    return json.dumps(_walk(d, counts), ensure_ascii=False, separators=(",", ":"))
