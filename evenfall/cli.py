import argparse
import os
import sys
import time

from . import config, index, ingest, wal


def _fmt_hit(r) -> str:
    cid, ts, kind, project, _src, snip, _score = r
    snip = " ".join(snip.split())
    return "[%d] %s %-9s %s | %s" % (cid, ts[:16].replace("T", " "), kind, project.replace(config.HOME_KEY + "-", "~/")[-28:], snip)


def cmd_ingest(a):
    print(ingest.ingest_all())


def cmd_index(a):
    db = index.connect()
    print(index.index_all(db))
    from . import brief
    print("briefs:", brief.build_all(db))


def cmd_sync(a):
    from . import hooks
    if a.scheduled and hooks.disabled():
        return
    t = time.time()
    s = ingest.ingest_all()
    if "skipped" in s:
        print(s)
        return
    db = index.connect()
    i = index.index_all(db)
    from . import brief
    brief.build_all(db)
    print({"ingest": {k: v for k, v in s.items() if k != "redactions"}, "redactions": s["redactions"], "index": i,
           "seconds": round(time.time() - t, 2)})


def cmd_find(a):
    from . import hooks
    db = index.connect()
    skip = None if a.include_current else hooks.current_session(os.getcwd())
    rows = index.find(db, " ".join(a.text), k=a.k, kind=a.kind, project=a.project, since=a.since, budget=a.budget,
                      exclude_session=skip)
    if not rows:
        print("no matches")
    for r in rows:
        print(_fmt_hit(r))


def cmd_show(a):
    db = index.connect()
    row = db.execute("select src, id from chunks where id=?", (a.id,)).fetchone()
    if not row:
        print("no such id")
        return
    src = row[0]
    for cid, ts, kind, text in db.execute(
            "select id, ts, kind, text from chunks where src=? and id between ? and ? order by id", (src, a.id - a.around, a.id + a.around)):
        mark = ">>" if cid == a.id else "  "
        print("%s [%d] %s %s\n%s\n" % (mark, cid, ts[:19].replace("T", " "), kind, text if cid == a.id else text[:600]))


def cmd_actions(a):
    from . import ledger
    db = index.connect()
    rows = ledger.actions(db, " ".join(a.text), since=a.since, project=a.project, tool=a.tool, k=a.k, only=a.status)
    if not rows:
        print("no matching actions")
    for lid, ts, st, tool, op, summ, why, req in rows:
        print("[%d] %s %-7s %s | %s%s" % (lid, ts[:16].replace("T", " "), st, op or tool, " ".join(summ.split())[:140], (" | why: " + why[:100]) if why else ""))


def cmd_trace(a):
    from . import ledger
    print(ledger.trace(index.connect(), a.id))


def cmd_audit(a):
    from . import ledger
    print(ledger.audit(index.connect()))


def cmd_prefs(a):
    from . import prefs
    db = index.connect()
    rows = prefs.find_prefs(db, " ".join(a.text), project=a.project, k=a.k, since=a.since)
    if not rows:
        print("no standing instructions found for that topic")
    for cid, ts, t in rows:
        print("[%d] %s %s" % (cid, ts[:10], t[:420]))


def cmd_design(a):
    from . import design
    db = index.connect()
    product = a.product or design.default_product(db, os.path.basename(os.getcwd().rstrip("/")))
    print(design.render(db, product, " ".join(a.text), a.all))


def cmd_alias(a):
    from . import design
    db = index.connect()
    if a.old and a.new:
        design.set_alias(db, a.old, a.new)
    print("aliases:", ", ".join("%s = %s" % kv for kv in sorted(design.aliases().items())))


def cmd_brief(a):
    from . import brief
    sys.stdout.write(brief.show(a.cwd or os.getcwd()))


def cmd_status(a):
    db = index.connect()
    ev = db.execute("select count(*), max(ts) from events").fetchone()
    ch = db.execute("select count(*) from chunks").fetchone()[0]
    arch = sum(p.stat().st_size for p in config.ARCHIVE.rglob("*.seg")) if config.ARCHIVE.exists() else 0
    src = sum(p.stat().st_size for p in config.SRC.rglob("*.jsonl")) if config.SRC.exists() else 0
    dbs = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
    print("events %d, chunks %d, newest event %s" % (ev[0], ch, ev[1]))
    print("source transcripts %.1f MB -> archive %.1f MB, index %.1f MB" % (src / 1e6, arch / 1e6, dbs / 1e6))
    print("machine", config.machine_id(), "home", config.HOME)


def cmd_verify(a):
    bad = 0
    total = 0
    for seg in sorted(config.ARCHIVE.rglob("*.seg")):
        good = wal.valid_length(seg)
        n = sum(1 for _ in wal.scan(seg))
        total += n
        if good != seg.stat().st_size:
            bad += 1
            print("TORN/CORRUPT TAIL:", seg, "valid", good, "size", seg.stat().st_size)
    print("segments checked, %d records, %d with a bad tail" % (total, bad))
    return 1 if bad else 0


def cmd_handoff(a):
    from . import handoff
    kw = a.project or os.path.basename(os.getcwd().rstrip("/"))
    print(handoff.draft(index.connect(), kw, a.since or ""))


def cmd_doctor(a):
    from . import doctor
    rows = doctor.run(index.connect(), deep=a.deep)
    print(doctor.render(rows))
    return 1 if any(r[0] == "FAIL" for r in rows) else 0


def cmd_off(a):
    config.STATE.mkdir(parents=True, exist_ok=True)
    (config.STATE / "disabled").write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
    print("evenfall is OFF: hooks, the watcher and the scheduled sync do nothing. `evenfall on` resumes (run `evenfall sync` to catch up).")


def cmd_on(a):
    try:
        (config.STATE / "disabled").unlink()
    except FileNotFoundError:
        pass
    print("evenfall is ON")


def cmd_hook(a):
    from . import hooks
    {"start": hooks.hook_start, "sync": hooks.hook_sync, "end": lambda: hooks.hook_sync(final=True)}[a.event]()


def cmd_watch(a):
    from . import hooks
    hooks.watch()


def cmd_rebuild(a):
    for suffix in ("", "-wal", "-shm"):
        p = str(config.DB_PATH) + suffix
        if os.path.exists(p):
            os.remove(p)
    cmd_index(a)


def main(argv=None):
    p = argparse.ArgumentParser(prog="evenfall")
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("ingest").set_defaults(fn=cmd_ingest)
    sp.add_parser("index").set_defaults(fn=cmd_index)
    sy = sp.add_parser("sync")
    sy.add_argument("--scheduled", action="store_true", help="called by launchd: do nothing while the kill switch is on")
    sy.set_defaults(fn=cmd_sync)
    f = sp.add_parser("find")
    f.add_argument("text", nargs="+")
    f.add_argument("-k", type=int, default=6)
    f.add_argument("--kind")
    f.add_argument("--project")
    f.add_argument("--since")
    f.add_argument("--budget", type=int, default=1800)
    f.add_argument("--include-current", action="store_true", help="also search the session running now (default: skipped, you already have it)")
    f.set_defaults(fn=cmd_find)
    s = sp.add_parser("show")
    s.add_argument("id", type=int)
    s.add_argument("--around", type=int, default=0)
    s.set_defaults(fn=cmd_show)
    b = sp.add_parser("brief")
    b.add_argument("--cwd")
    b.set_defaults(fn=cmd_brief)
    ac = sp.add_parser("actions", help="state-changing actions (cron, writes, deploys...) with ids for `trace`")
    ac.add_argument("text", nargs="*")
    ac.add_argument("-k", type=int, default=10)
    ac.add_argument("--since")
    ac.add_argument("--project")
    ac.add_argument("--tool")
    ac.add_argument("--status", choices=["ok", "error", "denied", "pending"])
    ac.set_defaults(fn=cmd_actions)
    tr = sp.add_parser("trace", help="why an action happened: request, reasoning, result, permission")
    tr.add_argument("id", type=int)
    tr.set_defaults(fn=cmd_trace)
    sp.add_parser("audit").set_defaults(fn=cmd_audit)
    dg = sp.add_parser("design", help="the user's design language: active design commands for a product, newest wins")
    dg.add_argument("text", nargs="*")
    dg.add_argument("--product")
    dg.add_argument("--all", action="store_true", help="include replaced and merged commands")
    dg.set_defaults(fn=cmd_design)
    al = sp.add_parser("alias", help="treat an old or staging product name as the same product: alias oldname NewName")
    al.add_argument("old", nargs="?")
    al.add_argument("new", nargs="?")
    al.set_defaults(fn=cmd_alias)
    pf = sp.add_parser("prefs", help="the user's standing instructions and design principles, optionally for a topic")
    pf.add_argument("text", nargs="*")
    pf.add_argument("-k", type=int, default=6)
    pf.add_argument("--project")
    pf.add_argument("--since")
    pf.set_defaults(fn=cmd_prefs)
    ho = sp.add_parser("handoff", help="draft a memory update from the ledger: what changed since the project memory was last edited")
    ho.add_argument("--project", help="memory keyword, default: this folder's name (acme, evenfall...)")
    ho.add_argument("--since")
    ho.set_defaults(fn=cmd_handoff)
    dc = sp.add_parser("doctor", help="health checks: hooks, lag, archive, errors, disk, retention, catch-up agent")
    dc.add_argument("--deep", action="store_true", help="verify every archive segment, not just the newest")
    dc.set_defaults(fn=cmd_doctor)
    sp.add_parser("off", help="kill switch: stop capturing and printing the card").set_defaults(fn=cmd_off)
    sp.add_parser("on", help="resume after `evenfall off`").set_defaults(fn=cmd_on)
    sp.add_parser("status").set_defaults(fn=cmd_status)
    sp.add_parser("verify").set_defaults(fn=cmd_verify)
    sp.add_parser("rebuild").set_defaults(fn=cmd_rebuild)
    h = sp.add_parser("hook")
    h.add_argument("event", choices=["start", "sync", "end"])
    h.set_defaults(fn=cmd_hook)
    sp.add_parser("watch").set_defaults(fn=cmd_watch)
    a, extra = p.parse_known_args(argv)
    if extra and a.cmd in ("find", "prefs", "design"):
        a.text += extra                      # words that start with a dash are search text, not options
    elif extra:
        p.error("unrecognized arguments: " + " ".join(extra))
    sys.exit(a.fn(a) or 0)
