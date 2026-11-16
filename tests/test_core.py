import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evenfall import brief, config, design, doctor, handoff, hooks, index, ingest, ledger, prefs, shell, wal  # noqa: E402
from evenfall import redact as redact_mod  # noqa: E402
from evenfall.redact import redact, redact_line  # noqa: E402


def point_config_at(root: Path):
    config.HOME = root / "home"
    config.SRC = root / "src"
    config.ARCHIVE = config.HOME / "archive"
    config.STATE = config.HOME / "state"
    config.DB_PATH = config.HOME / "evenfall.db"
    config.SRC.mkdir(parents=True)


def line(typ, text=None, ts="2026-11-06T10:00:00.000Z", sess="s1", blocks=None):
    msg = {"role": typ, "content": blocks if blocks is not None else text}
    return json.dumps({"type": typ, "timestamp": ts, "sessionId": sess, "cwd": "/tmp/proj", "message": msg})


def write_src(name, lines, newline_end=True):
    p = config.SRC / "-tmp-proj" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + ("\n" if newline_end else ""))
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        point_config_at(self.root)
        os.environ.pop("EVENFALL_CRASH", None)

    def tearDown(self):
        os.environ.pop("EVENFALL_CRASH", None)
        shutil.rmtree(self.root, ignore_errors=True)

    def records(self):
        out = []
        for seg in sorted(config.ARCHIVE.rglob("*.seg")):
            out += [r for r, _ in wal.scan(seg)]
        return out


class RedactTests(unittest.TestCase):
    def test_secrets_removed(self):
        for s in ["Authorization: Bearer abcdef0123456789abcdef0123", "key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA",
                  'password="hunter2hunter2"', "API_KEY=abc123def456", "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
                  "ghp_" + "a1B2c3D4e5" * 4]:
            self.assertIn("[REDACTED:", redact(s), s)

    def test_identifiers_and_prose_kept(self):
        keep = ["commit 602dff0 fixed it", "sha " + "9f2b7c1d4e5a60b8c3d2e1f0a9b8c7d6" * 2, "max_tokens: 100000",
                "the password is required for login", "token_file=/home/u1/x.json", "see app.acme.example/login"]
        for s in keep:
            self.assertEqual(redact(s), s)

    def test_paths_and_tool_names_kept_random_blob_redacted(self):
        for s in ["/private/tmp/claude-501/-Users-jo-Desktop-cl-Acme/021e905b-c133-42a3-a610-f70e27f131c6/scratchpad",
                  "mcp__claude_ai_Higgsfield__scene_builder_3d_create_project",
                  "Home-html/blog/one-hacker-ai-agents-100-websites"]:
            self.assertEqual(redact(s), s)
        self.assertIn("[REDACTED:blob:", redact("value iK2ZWeqhFWCEPyYngFb51yBMWXaSCrUZoL8g5ubb end"))

    def test_json_stays_valid_after_redaction(self):
        secret = "iK2ZWeqhFWCEPyYngFb51yBMWXaSCrUZoL8g5ubb"
        for esc in ["\\n", "\\t", "\\r", '\\"', "\\\\", "\\u00e9"]:
            raw = json.dumps({"a": "x" + json.loads('"%s"' % esc) + secret + " y", "b": "password=Sup3rSecret99 ok",
                              "c": "Bearer abcdef0123456789abcdef0123"})
            out = redact(raw)
            json.loads(out)
            self.assertNotIn(secret, out)

    def test_fallback_keeps_json_valid_if_regex_ever_breaks_it(self):
        raw = json.dumps({"a": "key iK2ZWeqhFWCEPyYngFb51yBMWXaSCrUZoL8g5ubb", "b": "ok"})
        real = redact_mod.redact
        calls = {"n": 0}

        def broken(text, counts=None):
            calls["n"] += 1
            return real(text, counts) + "\\[" if calls["n"] == 1 else real(text, counts)   # first (whole-line) call corrupts
        redact_mod.redact = broken
        try:
            counts = {}
            out = redact_line(raw, counts)
        finally:
            redact_mod.redact = real
        json.loads(out)
        self.assertNotIn("iK2ZWeqhFWCEPyYngFb51yBMWXaSCrUZoL8g5ubb", out)
        self.assertEqual(counts.get("json_fallback"), 1)

    def test_same_secret_same_token(self):
        a = redact("password=Sup3rSecret99"); b = redact("password=Sup3rSecret99")
        self.assertEqual(a, b)
        self.assertNotIn("Sup3rSecret99", a)


class WalTests(Base):
    def test_torn_tail_every_offset(self):
        path = self.root / "t.seg"
        s = wal.Segment(path)
        for i in range(5):
            s.append({"n": i, "pad": "x" * 50})
        s.write_out(); s.sync(); s.close()
        full = path.read_bytes()
        last_start = wal.valid_length(path)
        # find the start of the last record by rescanning the first four
        ends = [e for _, e in wal.scan(path)]
        boundary = ends[3]
        for cut in range(boundary, len(full)):
            path.write_bytes(full[:cut])
            got = [r["n"] for r, _ in wal.scan(path)]
            self.assertEqual(got, [0, 1, 2, 3], "cut at %d" % cut)
            seg = wal.Segment(path)            # reopening repairs the torn tail
            seg.append({"n": 99}); seg.write_out(); seg.sync(); seg.close()
            self.assertEqual([r["n"] for r, _ in wal.scan(path)], [0, 1, 2, 3, 99])
        path.write_bytes(full)
        self.assertEqual(last_start, len(full))

    def test_garbage_in_middle_stops_scan(self):
        path = self.root / "g.seg"
        s = wal.Segment(path)
        for i in range(3):
            s.append({"n": i})
        s.write_out(); s.sync(); s.close()
        b = bytearray(path.read_bytes())
        b[-3] ^= 0xFF
        path.write_bytes(bytes(b))
        self.assertEqual([r["n"] for r, _ in wal.scan(path)], [0, 1])


class IngestTests(Base):
    def test_idempotent_and_incremental(self):
        p = write_src("a.jsonl", [line("user", "hello"), line("assistant", blocks=[{"type": "text", "text": "hi"}])])
        s1 = ingest.ingest_all()
        self.assertEqual(s1["lines"], 2)
        self.assertEqual(ingest.ingest_all()["lines"], 0)
        with open(p, "a") as f:
            f.write(line("user", "third") + "\n")
        self.assertEqual(ingest.ingest_all()["lines"], 1)
        self.assertEqual(len(self.records()), 3)

    def test_partial_line_waits_for_newline(self):
        p = write_src("a.jsonl", [line("user", "one")])
        ingest.ingest_all()
        with open(p, "a") as f:
            f.write(line("user", "two")[:30])         # half-written line
        self.assertEqual(ingest.ingest_all()["lines"], 0)
        with open(p, "a") as f:
            f.write(line("user", "two")[30:] + "\n")
        self.assertEqual(ingest.ingest_all()["lines"], 1)
        db = index.connect(); index.index_all(db)
        self.assertEqual(db.execute("select count(*) from chunks where kind='user'").fetchone()[0], 2)

    def test_replaced_file_gets_new_generation(self):
        write_src("a.jsonl", [line("user", "old one"), line("user", "old two")])
        ingest.ingest_all()
        write_src("a.jsonl", [line("user", "new")])
        s = ingest.ingest_all()
        self.assertEqual((s["lines"], s["restarts"]), (1, 1))
        db = index.connect(); index.index_all(db)
        self.assertEqual(db.execute("select count(*) from chunks where kind='user'").fetchone()[0], 3)

    def test_crash_at_every_point_loses_nothing(self):
        for point in ["after_write", "after_sync", "after_cursor"]:
            with self.subTest(point=point):
                shutil.rmtree(self.root, ignore_errors=True)
                self.root = Path(tempfile.mkdtemp()); point_config_at(self.root)
                lines = [line("user", "msg %d" % i, ts="2026-11-0%dT10:00:00Z" % (1 + i % 3)) for i in range(30)]
                write_src("a.jsonl", lines)
                os.environ["EVENFALL_CRASH"] = point
                with self.assertRaises(ingest.SimulatedCrash):
                    ingest.ingest_all()
                os.environ.pop("EVENFALL_CRASH")
                # power loss may also have dropped any unflushed tail: emulate by cutting a segment tail at random spots
                if point == "after_write":
                    for seg in config.ARCHIVE.rglob("*.seg"):
                        seg.write_bytes(seg.read_bytes()[: max(len(wal.MAGIC), seg.stat().st_size // 2)])
                ingest.ingest_all()
                db = index.connect(); index.index_all(db)
                got = db.execute("select count(*) from events").fetchone()[0]
                self.assertEqual(got, 30, "lost or duplicated events after crash at %s" % point)

    def test_lock_prevents_double_run(self):
        import fcntl
        config.STATE.mkdir(parents=True, exist_ok=True)
        fd = os.open(config.STATE / "lock", os.O_WRONLY | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.assertIn("skipped", ingest.ingest_all())
        finally:
            os.close(fd)


class SearchTests(Base):
    def test_find_brief_and_identifiers(self):
        digest = "9f2b7c1d4e5a60b8c3d2e1f0a9b8c7d6"
        write_src("a.jsonl", [
            line("user", "why are mailboxes at risk on app.acme.example"),
            line("assistant", blocks=[{"type": "text", "text": "Bounce rate is over 8 percent on two mailboxes."},
                                      {"type": "tool_use", "name": "mcp__plugin_cloudhost_cloudhost__execute", "input": {"operation": "hosting_cron-jobs_list"}}]),
            line("user", blocks=[{"type": "tool_result", "content": "cron list ok", "is_error": False}]),
            line("user", "note the reference " + digest + " for later", ts="2026-11-07T10:00:00Z"),
        ])
        ingest.ingest_all()
        db = index.connect(); index.index_all(db); brief.build_all(db)
        hits = index.find(db, "mailbox bounce risk")
        self.assertTrue(any("ounce" in h[5] for h in hits), "the bounce-rate chunk must be among the hits")
        self.assertEqual(index.find(db, "mailboxes", kind="assistant")[0][2], "assistant")
        self.assertTrue(index.find(db, digest), "pure hex identifiers must stay searchable")
        card = brief.show("/tmp/proj")
        self.assertIn("evenfall", card)
        self.assertIn("Connected services used (MCP): plugin_cloudhost_cloudhost", card)
        self.assertIn("acme.example", card)


def tool_use(tid, name, inp, text=None):
    blocks = ([{"type": "text", "text": text}] if text else []) + [{"type": "tool_use", "id": tid, "name": name, "input": inp}]
    return line("assistant", blocks=blocks)


def tool_result(tid, content, err=False):
    return line("user", blocks=[{"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": err}])


class LedgerTests(Base):
    def make(self):
        return [
            line("user", "please clean up the temp folder and schedule the diagnostic"),
            tool_use("t1", "Bash", {"command": "rm -rf /tmp/x"}, "Why: stale files break the build. Alternative rejected: leave them."),
            tool_result("t1", "done"),
            tool_use("t2", "mcp__plugin_cloudhost_cloudhost__execute",
                     {"operation": "hosting_cron-jobs_create", "params": {"username": "u12345", "time": "* * * * *"}}, "Creating the cron job."),
            tool_result("t2", "Permission for this action was denied by the Claude Code auto mode classifier.", err=True),
            tool_use("t3", "Bash", {"command": "ls -la 2>&1 | head"}),
            tool_result("t3", "files"),
            tool_use("t4", "mcp__plugin_cloudhost_cloudhost__execute", {"operation": "hosting_cron-jobs_list", "params": {"username": "u12345"}}),
            tool_result("t4", "[]"),
            tool_use("t5", "Write", {"file_path": "/tmp/notes.md", "content": "x"}),
            tool_result("t5", "ok"),
            tool_use("t6", "Bash", {"command": "echo hi > /tmp/f && git status"}),
            tool_result("t6", "ok"),
            tool_use("t7", "Bash", {"command": "git commit -m wip"}),
            tool_result("t7", "ok"),
        ]

    def test_classification_linking_and_trace(self):
        write_src("a.jsonl", self.make())
        ingest.ingest_all()
        db = index.connect(); index.index_all(db)
        self.assertEqual(db.execute("select count(*) from ledger").fetchone()[0], 5)          # rm, cron create, Write, redirect, commit
        self.assertEqual(db.execute("select count(*) from ledger where summary like '%cron-jobs_list%'").fetchone()[0], 0)
        self.assertEqual(db.execute("select count(*) from ledger where summary like '%ls -la%'").fetchone()[0], 0)
        rm = db.execute("select id, status, why, request_id from ledger where summary like 'rm -rf%'").fetchone()
        self.assertEqual(rm[1], "ok")
        self.assertEqual(rm[2], "stale files break the build. Alternative rejected: leave them.")
        self.assertIn("clean up the temp folder", db.execute("select text from chunks where id=?", (rm[3],)).fetchone()[0])
        cron = db.execute("select id, status from ledger where op like '%cron-jobs_create%'").fetchone()
        self.assertEqual(cron[1], "denied")
        t = ledger.trace(db, cron[0])
        self.assertIn("DENIED", t); self.assertIn("schedule the diagnostic", t); self.assertIn("Creating the cron job", t)
        self.assertIn("state-changing actions: 5", ledger.audit(db))
        self.assertTrue(ledger.actions(db, "cron", k=5))

    def test_pending_result_settles_on_next_run(self):
        p = write_src("a.jsonl", [line("user", "deploy it"), tool_use("t1", "Bash", {"command": "rsync -a src/ host:dst/"})])
        ingest.ingest_all()
        db = index.connect(); index.index_all(db)
        self.assertEqual(db.execute("select status from ledger").fetchone()[0], "pending")
        with open(p, "a") as f:
            f.write(tool_result("t1", "sent 3 files") + "\n")
        ingest.ingest_all(); index.index_all(db)
        self.assertEqual(db.execute("select status from ledger").fetchone()[0], "ok")

    def test_command_names_respect_quotes(self):
        names = shell.command_names('cd x && grep -nE "at_risk|atRisk" f.php | head -3; FOO=1 ssh host "ls | wc"; for i in 1; do echo $i; done')
        self.assertEqual(names, ["cd", "grep", "head", "ssh", "echo"])

    def test_heredoc_bodies_and_assignments_are_not_commands(self):
        cmd = "S=~/x && cp a b && python3 - <<'E'\nassert True\nprint(1)\nE\nrsync -a src/ host:dst/"
        self.assertEqual(shell.command_names(cmd), ["cp", "python3", "rsync"])
        self.assertEqual(shell.remote_op(cmd), "rsync")
        self.assertIsNone(shell.remote_op("cp a b && echo done"))
        self.assertIsNone(shell.remote_op("curl -s http://127.0.0.1:8099/health"))
        self.assertEqual(shell.remote_op("curl -s https://api.example.com/x"), "curl")

    def test_why_line_must_start_a_line(self):
        self.assertIsNone(ledger.WHY.search("the `Why:` convention is a rule"))
        self.assertEqual(ledger.WHY.search("Creating it.\n**Why:** need it. Alternative rejected: none.").group(1), "need it. Alternative rejected: none.")

    def test_local_edits_are_labelled_local(self):
        mut, op, _ = ledger.classify("Bash", json.dumps({"command": "cd /x && sed -i '' s/a/b/ f.py"}))
        self.assertEqual((mut, op), (True, "local:sed"))
        mut, op, _ = ledger.classify("Bash", json.dumps({"command": "git -C /x push origin main"}))
        self.assertEqual((mut, op), (True, "git push"))

    def test_standing_instructions_are_found_by_topic(self):
        write_src("a.jsonl", [
            line("user", "what time is it in Chicago right now, can you tell me quickly"),
            line("user", "the buttons look cluttered and I don't want inline styles, keep them aligned and darker", ts="2026-11-05T10:00:00Z"),
            line("user", "make sure the emails always go out only in the send window, never on weekends", ts="2026-11-04T10:00:00Z"),
            line("user", "ok thanks"),
        ])
        ingest.ingest_all()
        db = index.connect(); index.index_all(db)
        got = prefs.find_prefs(db, "button design ui")
        self.assertEqual(len(got), 1)
        self.assertIn("buttons look cluttered", got[0][2])
        allp = [t for _i, _ts, t in prefs.find_prefs(db)]
        self.assertTrue(any("send window" in t for t in allp))
        self.assertFalse(any("what time is it" in t for t in allp))

    def test_empty_output_result_settles_as_ok(self):
        write_src("a.jsonl", [line("user", "touch it"), tool_use("t1", "Bash", {"command": "touch /tmp/zz"}), tool_result("t1", "")])
        ingest.ingest_all()
        db = index.connect(); index.index_all(db)
        self.assertEqual(db.execute("select status from ledger").fetchone()[0], "ok")

    def test_environment_map(self):
        write_src("a.jsonl", self.make())
        ingest.ingest_all()
        db = index.connect(); index.index_all(db)
        env = "\n".join(brief.build_env(db, "-tmp-proj"))
        self.assertIn("plugin_cloudhost_cloudhost", env)
        self.assertIn("u12345", env)
        card = brief.build_project(db, "-tmp-proj")
        self.assertIn("Last state-changing actions", card)
        self.assertIn("why: stale files break the build", card)


class DesignTests(Base):
    def build(self, lines):
        write_src("a.jsonl", lines)
        ingest.ingest_all()
        db = index.connect(); index.index_all(db)
        return db

    def status(self, db, text_part):
        return db.execute("select status, followed, repeats, product from design_rules where text like ?", ("%" + text_part + "%",)).fetchone()

    def test_followed_newer_command_overrides_older(self):
        db = self.build([
            line("user", "make it desktop only, skip mobile UI for now", ts="2026-11-01T10:00:00Z"),
            line("assistant", blocks=[{"type": "text", "text": "ok"}]),
            line("user", "make everything mobile responsive", ts="2026-11-03T10:00:00Z"),
            tool_use("t1", "Write", {"file_path": "/Users/x/Desktop/cl/AcmeApp/public_html/index.php", "content": "x"}, "Adding the phone layout."),
            tool_result("t1", "ok"),
            line("user", "thanks that works", ts="2026-11-03T11:00:00Z"),
        ])
        old, new = self.status(db, "desktop only"), self.status(db, "mobile responsive")
        self.assertEqual(old[0], "overridden")
        self.assertEqual((new[0], new[1], new[3]), ("active", 1, "AcmeApp"))

    def test_unfollowed_newer_command_only_marks_pending(self):
        db = self.build([
            line("user", "I want something darker here", ts="2026-11-01T10:00:00Z"),
            line("user", "actually switch to a light theme please", ts="2026-11-02T10:00:00Z"),
            line("assistant", blocks=[{"type": "text", "text": "Noted, thinking about it."}]),
        ])
        self.assertEqual(self.status(db, "darker")[0], "pending_override")
        self.assertEqual(self.status(db, "light theme")[0], "active")

    def test_repeat_merges_and_counts_and_unrelated_topics_do_not_conflict(self):
        db = self.build([
            line("user", "the buttons are not aligned, I want them aligned", ts="2026-11-01T10:00:00Z"),
            line("user", "still not aligned, keep the pills aligned", ts="2026-11-02T10:00:00Z"),
            line("user", "I don't need microanimations here", ts="2026-11-02T11:00:00Z"),
            line("user", "there should be a subtle loading animation", ts="2026-11-02T12:00:00Z"),
        ])
        rows = db.execute("select status, repeats from design_rules where topic='alignment' order by ts").fetchall()
        self.assertEqual([r[0] for r in rows], ["merged", "active"])
        self.assertEqual(rows[1][1], 1)
        self.assertEqual(db.execute("select status from design_rules where topic='micro_animation'").fetchone()[0], "active")
        self.assertEqual(db.execute("select status from design_rules where topic='loading_animation'").fetchone()[0], "active")

    def test_negation_flips_mobile_and_alias_groups_old_product_names(self):
        config.STATE.mkdir(parents=True, exist_ok=True)
        (config.STATE / "aliases.json").write_text(json.dumps({"oldname": "AcmeApp"}))     # aliases are user data, not code
        db = self.build([
            line("user", "I am not going to open this application on mobile so do not worry about mobile UI", ts="2026-11-01T10:00:00Z"),
            tool_use("t0", "Edit", {"file_path": "/Users/x/Desktop/cl/oldname/leaddesk/public_html/index.php"}),
            tool_result("t0", "ok"),
            line("user", "make everything mobile responsive", ts="2026-11-03T10:00:00Z"),
            tool_use("t1", "Edit", {"file_path": "/Users/x/Desktop/cl/AcmeApp/public_html/index.php"}),
            tool_result("t1", "ok"),
        ])
        old = db.execute("select stance, status, product from design_rules where text like '%not going to open%'").fetchone()
        self.assertEqual((old[0], old[1], old[2]), (-1, "overridden", "oldname"))        # oldname is the old name of AcmeApp
        self.assertIn("make everything mobile responsive", design.render(db, "AcmeApp"))
        self.assertIn("make everything mobile responsive", design.render(db, "oldname"))

    def test_complaint_about_missing_mobile_support_is_not_a_rejection(self):
        got = {seg: st for _t, _s, st, seg in design.atoms("AcmeApp is not very mobile optimised. I am not going to open this on mobile so do not worry about mobile UI")}
        self.assertEqual(got["AcmeApp is not very mobile optimised"], 1)
        self.assertEqual([st for seg, st in got.items() if "not going to open" in seg], [-1])

    def test_questions_and_chatter_are_not_rules(self):
        db = self.build([line("user", "what is a dark theme and how does it work?"), line("user", "ok thanks, looks fine"),
                         line("user", "Should I buy more domains for more emails?"), line("user", "79 emails but the graph shows we have followed up"),
                         line("user", "hello can you check the server status for the portal")])
        self.assertEqual(db.execute("select count(*) from design_rules").fetchone()[0], 0)

    def test_render_and_card_section(self):
        db = self.build([
            line("user", "I would want something darker here, and the font should be smaller and crisper", ts="2026-11-01T10:00:00Z"),
            tool_use("t1", "Edit", {"file_path": "/Users/x/Desktop/cl/AcmeApp/public_html/index.php"}),
            tool_result("t1", "ok"),
            line("user", "strip it down, nothing unnecessary", ts="2026-11-01T11:00:00Z"),
        ])
        out = design.render(db, "AcmeApp")
        self.assertIn("darker", out); self.assertIn("crisper", out); self.assertIn("unnecessary", out)
        sec = design.section(db, "AcmeApp")
        self.assertIn("Design language for AcmeApp", sec)
        self.assertEqual(design.default_product(db, "nothing-here"), "AcmeApp")


class HandoffTests(Base):
    def setUp(self):
        super().setUp()
        config.MEMORY_DIR = self.root / "memory"
        config.MEMORY_DIR.mkdir()
        self.mem = config.MEMORY_DIR / "project_acme_portal.md"
        self.mem.write_text("---\nname: acme-portal\nmetadata:\n  modified: 2026-11-01T00:00:00.000Z\n---\nold state\n")
        os.utime(self.mem, (1793491200, 1793491200))                                               # 2026-11-01: older than the actions below
        lines = [line("user", "ship the acme change")]
        for i in range(7):
            lines += [tool_use("p%d" % i, "Bash", {"command": 'cd ~/acme && git commit -q -m "acme change %d" && git push' % i},
                               "Why: deploy acme change %d. Alternative rejected: wait." % i if i == 0 else None), tool_result("p%d" % i, "ok")]
        lines += [tool_use("q", "Bash", {"command": "ssh deploy@other.example.com systemctl restart nginx"}), tool_result("q", "ok")]
        write_src("a.jsonl", lines)
        ingest.ingest_all()
        self.db = index.connect(); index.index_all(self.db)

    def test_keyword_comes_from_the_slug_and_counts_whole_words(self):
        self.assertEqual([k for k, _p, _m in handoff.memories()], ["acme"])
        self.assertEqual(len(handoff._since_rows(self.db, "acme", "2026-11-01T00:00:00")), 7)    # the ssh action does not mention acme
        self.assertEqual(len(handoff._since_rows(self.db, "acm", "2026-11-01T00:00:00")), 0)      # whole words only

    def test_staleness_line_appears_when_behind_and_clears_after_an_edit(self):
        self.assertIn("project_acme_portal.md is 7 outward actions behind", handoff.staleness_line(self.db, "/users/x/acme"))
        self.assertEqual(handoff.staleness_line(self.db, "/users/x/other"), "")                    # unrelated folder: no notice
        self.mem.write_text(self.mem.read_text() + "\nupdated\n")                                  # an edit moves the file's mtime past the actions
        os.utime(self.mem, (4102444800, 4102444800))
        self.assertEqual(handoff.staleness_line(self.db, "/users/x/acme"), "")

    def test_draft_groups_by_reason_and_lists_commit_messages(self):
        d = handoff.draft(self.db, "acme")
        self.assertIn("7 outward actions", d)
        self.assertIn("deploy acme change 0. Alternative rejected: wait.", d)
        self.assertIn("commit: acme change 3", d)
        m = handoff.COMMIT_MSG.search('cd ~/acme && git commit -q -am "combined flags work" && git push')
        self.assertEqual(m.group(1), "combined flags work")
        self.assertEqual(handoff.COMMIT_MSG.search("git commit --message='long form'").group(2), "long form")
        self.assertIn("git push x", d)
        self.assertIn("No outward-facing actions", handoff.draft(self.db, "nothing-here"))


class SafetyTests(Base):
    def setUp(self):
        super().setUp()
        config.STATE.mkdir(parents=True, exist_ok=True)

    def test_hook_failure_is_logged_not_raised_and_stdout_stays_empty(self):
        import io, contextlib
        real = brief.show
        brief.show = lambda cwd: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                hooks.hook_start()
        finally:
            brief.show = real
        self.assertEqual(out.getvalue(), "")
        log = (config.STATE / "hook-errors.log").read_text()
        self.assertIn("hook start", log); self.assertIn("RuntimeError: boom", log)
        self.assertEqual(len(doctor.recent_errors(24)), 1)

    def test_error_log_rotates(self):
        (config.STATE / "hook-errors.log").write_text("x" * (hooks.ERR_LOG_MAX + 1))
        hooks._fail("again")
        self.assertTrue((config.STATE / "hook-errors.log.1").exists())
        self.assertLess((config.STATE / "hook-errors.log").stat().st_size, 5000)

    def test_kill_switch_silences_the_card_and_the_session_record(self):
        import io, contextlib
        (config.STATE / "disabled").write_text("x")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks.hook_start()
        self.assertEqual(out.getvalue(), "")
        self.assertFalse((config.STATE / "current").exists())
        self.assertTrue(hooks.disabled())

    def test_doctor_flags_missing_hooks_and_accepts_installed_ones(self):
        cfg = self.root / "settings.json"
        doctor.SETTINGS = cfg
        cfg.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": brief.BIN + " hook start"}]}]}}))
        self.assertEqual(doctor.hooks_installed(), ["PreCompact", "SessionEnd"])
        hk = {ev: [{"hooks": [{"type": "command", "command": brief.BIN + " hook x"}]}] for ev in doctor.EVENTS}
        cfg.write_text(json.dumps({"hooks": hk, "cleanupPeriodDays": 3650}))
        self.assertEqual(doctor.hooks_installed(), [])

    def test_lag_is_measured_against_the_newest_transcript(self):
        db = index.connect()
        self.assertEqual(doctor.lag_seconds(db), 0.0)                       # nothing to capture yet
        f = write_src("a.jsonl", [line("user", "hello")])
        ingest.ingest_all(); index.index_all(db)
        os.utime(f, (4102444800, 4102444800))                               # the transcript kept growing long after the last archived event
        self.assertGreater(doctor.lag_seconds(db), doctor.LAG_FAIL)
        cfg = self.root / "settings.json"; doctor.SETTINGS = cfg
        cfg.write_text(json.dumps({"hooks": {}}))
        names = {n for _l, n, _d in doctor.quick(db)}
        self.assertEqual(names, {"hooks", "lag"})
        self.assertIn("Evenfall warning", doctor.card_warning(db))


if __name__ == "__main__":
    unittest.main()
