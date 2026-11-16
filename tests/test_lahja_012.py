# -*- coding: utf-8 -*-
"""Regression tests for lahja 0.1.2 (worker hardening, scope note, regex guard, pack quality).
All sample sentences are invented for the tests."""
import json
import os
import subprocess
import sys
import time
import unittest

from test_lahja import (SCRIPT, TempDirCase, lahja, lint, pack_of, rule, run_cli)

SLOW = "(?:x*)(?:x*)(?:x*)(?:x*)z"   # passes the heuristic guard, still very slow on a long run of x


def worker_env(token):
    env = dict(os.environ)
    env[lahja.TOKEN_ENV] = token
    return env


class WorkerTests(TempDirCase):
    def slow_pack(self):
        self.write_pack("slow", {"id": "slow", "label": "x",
                                 "rules": [rule(kind="numeral", id="num"), rule(id="slow-rule", pattern=SLOW)]})
        self.write("ok.txt", "1 abc\n")
        self.write("slow.txt", "x" * 1500 + "\n")

    def test_hand_run_worker_is_refused(self):
        for extra in ([], ["--pack", "saudi", "--", "a.txt"]):
            code, out, err = run_cli(["--_worker"] + extra, timeout=30)
            self.assertEqual(code, 1)
            self.assertIn("internal", err)
            self.assertEqual(out, "")

    def test_worker_with_wrong_token_or_order_is_refused(self):
        for order in (b"", b"{}", b'{"token": "nope", "argv": []}'):
            done = subprocess.run([sys.executable, "-X", "utf8", SCRIPT, "--_worker"], input=order,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=worker_env("right"), timeout=30)
            self.assertEqual(done.returncode, 1)
            self.assertIn(b"internal", done.stderr)

    def test_worker_ends_by_itself_at_its_own_deadline(self):
        self.slow_pack()
        order = json.dumps({"token": "t", "argv": ["--pack", "slow", "--pack-dir", self.dir,
                                                   "--timeout", "1", "--", self.dir + "/slow.txt"]})
        start = time.time()
        done = subprocess.run([sys.executable, "-X", "utf8", SCRIPT, "--_worker"], input=order.encode(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=worker_env("t"),
                              timeout=60)
        self.assertLess(time.time() - start, 30)
        self.assertEqual(done.returncode, 1)

    def test_orphaned_worker_does_not_outlive_its_limit(self):
        self.slow_pack()
        parent = subprocess.Popen([sys.executable, "-X", "utf8", SCRIPT, "--pack", "slow", "--pack-dir",
                                   self.dir, "--timeout", "1", "--", self.dir + "/slow.txt"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  stdin=subprocess.DEVNULL)
        time.sleep(0.4)
        parent.kill()
        start = time.time()
        parent.stdout.read()      # returns only when every holder of the pipe, the worker too, is gone
        self.assertLess(time.time() - start, 30)
        parent.wait()

    def test_many_long_file_names_still_run_under_the_time_limit(self):
        path = self.write("a.txt", "[a] 5\n")
        code, out, err = run_cli(["--all-lines", "--", ] + [path] * 250, timeout=120)
        self.assertEqual(code, 2)
        self.assertNotIn("stopped", err)

    def test_timeout_keeps_earlier_findings_and_names_file_and_rule(self):
        self.slow_pack()
        args = ["--pack", "slow", "--pack-dir", self.dir, "--timeout", "2", "--"]
        files = [self.dir + "/ok.txt", self.dir + "/slow.txt"]
        code, out, err = run_cli(args + files, timeout=60)
        self.assertEqual(code, 1)
        self.assertIn("ok.txt:1:1: error slow/num", out)
        self.assertIn("stopped after 2 seconds", err)
        self.assertIn("slow.txt", err)
        self.assertIn("slow/slow-rule", err)
        self.assertIn("1 of 2 file(s)", err)
        code, out, err = run_cli(["--format", "json"] + args + files, timeout=60)
        data = json.loads(out)
        self.assertFalse(data["complete"])
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["summary"]["tool_errors"], 1)

    def test_device_names_are_refused_as_inputs(self):
        for name in ("CON", "nul", "AUX", "COM1", "con.txt", "x/LPT2", "//./pipe/x"):
            self.assertTrue(lahja.is_device_name(name), name)
        self.assertFalse(lahja.is_device_name("console.txt"))
        for name in ("CON", "NUL", "AUX"):
            start = time.time()
            code, out, err = run_cli(["--", name], timeout=60)
            self.assertEqual(code, 1, name)
            self.assertIn("device", err)
            self.assertLess(time.time() - start, 20)


class ScopeNoteTests(TempDirCase):
    def tagged_pack(self):
        return pack_of(rule(kind="numeral"), scope={"mode": "tagged", "tag_pattern": r"^\s*\[[^\]]+\]"})

    def lint(self, text):
        pack = self.tagged_pack()
        stats = {}
        found, checked = lahja.lint_text(text, pack, None, None, None, stats)
        return found, checked, stats

    def test_skipped_lines_are_counted(self):
        found, checked, stats = self.lint("[a] 1\n\nplain 2\n# head 3\n")
        self.assertEqual((len(found), checked, stats["skipped_untagged"]), (1, 1, 2))

    def test_invisible_characters_before_the_tag_are_tolerated(self):
        for lead in ("‏", "‎", "​", "﻿", " ", " ‏ ", "؜"):
            found, checked, stats = self.lint(lead + "[a] 7")
            self.assertEqual(len(found), 1, repr(lead))
            self.assertEqual(stats["skipped_untagged"], 0)
            self.assertEqual(found[0]["col"], len(lead) + 5)

    def test_cli_summary_and_json_header_report_skipped_lines(self):
        path = self.write("m.txt", "[a] تمام\nهذي سطر ثاني 30\n")
        code, out, err = run_cli(["--", path])
        self.assertIn("1 untagged line(s) skipped (use --all-lines)", out)
        code, out, err = run_cli(["--format", "json", "--", path])
        data = json.loads(out)
        self.assertEqual(data["untagged_skipped"], 1)
        self.assertEqual(data["scope_note"], "1 untagged line(s) skipped (use --all-lines)")
        code, out, err = run_cli(["--all-lines", "--format", "json", "--", path])
        data = json.loads(out)
        self.assertEqual((data["untagged_skipped"], data["scope_note"]), (0, None))


class GuardTests(unittest.TestCase):
    def test_demonstrated_bypasses_are_refused(self):
        for pat in ("a?" * 28 + "a" * 28, "(a?)" * 30 + "a" * 30, "a{0,1000}" * 30 + "b",
                    ".*" * 6 + "x", r"\w*\d*" * 3 + "z", "(?=(?:a+))a*a*a*a*a*b",
                    r"[؀-ۿ\w\W]*" * 6 + r"\u0001"):
            self.assertIsNotNone(lahja.regex_problem(pat), pat[:40])

    def test_normal_patterns_still_pass(self):
        for pat in (lahja.DEFAULT_TAG_PATTERN, r"^\s*\[[a-z ]+\]\s*(.*)$", r"\d+\s*kg", r"a{2,5}b?"):
            self.assertIsNone(lahja.regex_problem(pat), pat)

    def test_guard_is_documented_as_a_heuristic(self):
        self.assertIn("HEURISTIC", lahja.regex_problem.__doc__)
        self.assertIsNone(lahja.regex_problem(SLOW))

    def test_wordlist_benchmark(self):
        rules = []
        for r in range(40):
            words = ["w%dq%d" % (r, i) for i in range(499)] + ["target"]
            rules.append(rule(id="r%d" % r, kind="wordlist", words=words, prefixes=["و", "ف"]))
        line = ("كلمة عادية " * 150 + "target")[:1800]
        text = "\n".join([line] * 20)
        start = time.time()
        pack = pack_of(*rules)
        found, _ = lahja.lint_text(text, pack)
        self.assertLess(time.time() - start, 4.0)
        self.assertEqual(len(found), 500 + 1)   # 40 rules x 20 lines hits, capped


class LowItemTests(TempDirCase):
    def test_ids_with_a_trailing_newline_are_refused(self):
        self.assertIsNone(lahja.NAME_RE.match("abc\n"))
        with self.assertRaises(lahja.PackError):
            lahja.validate_pack({"id": "t\n", "label": "x", "rules": [rule(pattern="a")]})
        with self.assertRaises(lahja.PackError):
            lahja.validate_pack({"id": "t", "label": "x", "rules": [rule(pattern="a", id="r\n")]}, "t")
        for bad in ("con", "nul", "aux", "com1"):
            self.assertFalse(lahja.valid_pack_name(bad))

    def test_tag_pattern_is_sanitised_in_json(self):
        self.write_pack("t", {"id": "t", "label": "x", "rules": [rule(kind="numeral")],
                              "scope": {"mode": "tagged", "tag_pattern": "^‮ \u0007\\[x\\]"}})
        code, out, err = run_cli(["--pack", "t", "--pack-dir", self.dir, "--format", "json", "--",
                                  self.write("a.txt", "[x] 1\n")])
        for ch in ("‮", " "):
            self.assertNotIn(ch, out)
        self.assertEqual(json.loads(out)["scope"]["mode"], "tagged")

    def test_long_line_warnings_respect_cap_and_filter(self):
        pack = pack_of(rule(kind="numeral"))
        found, _ = lahja.lint_text("\n".join(["a" * 2100] * 900), pack)
        self.assertEqual(found[-1]["rule"], "lahja/too-many-findings")
        self.assertEqual(len(found), 501)
        found, _ = lahja.lint_text("# note " + "a" * 2100 + "\nSPOKEN 5", pack,
                                   __import__("re").compile("^SPOKEN"))
        self.assertEqual([f["line"] for f in found], [2])

    def test_pack_file_name_case_is_exact(self):
        self.write_pack("Saudi", {"id": "saudi", "label": "x", "rules": [rule(kind="numeral")]})
        names = os.listdir(self.dir)
        code, out, err = run_cli(["--list-packs", "--pack-dir", self.dir])
        self.assertIn("INVALID", out)
        if "Saudi.json" in names:
            code, out, err = run_cli(["--pack", "saudi", "--pack-dir", self.dir, "--format", "json", "--",
                                      self.write("a.txt", "[a] 1\n")])
            self.assertEqual(json.loads(out)["scope"]["mode"], "tagged")   # the built-in one
        self.assertEqual(run_cli(["--pack", "Saudi", "--", "a.txt"])[0], 1)

    def test_list_packs_says_how_many_were_skipped(self):
        for i in range(lahja.MAX_PACKS_LISTED + 5):
            self.write_pack("p%03d" % i, {"id": "p%03d" % i, "label": "x", "rules": [rule(kind="numeral")]})
        code, out, err = run_cli(["--list-packs", "--pack-dir", self.dir])
        self.assertIn("5 more pack file(s) not listed", out)
        code, out, err = run_cli(["--list-packs", "--pack-dir", self.dir, "--format", "json"])
        self.assertEqual(json.loads(out)["skipped"], 5)


class SaudiQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = lahja.find_pack("saudi")

    def ids(self, text):
        scope = lahja.effective_scope(self.pack, "all")
        return [f["rule"].split("/")[1] for f in lahja.lint_text(text, self.pack, None, scope)[0]]

    def test_missed_forms_are_found(self):
        self.assertEqual(self.ids("مرت والسنوات بسرعة"), ["formal-plurals"])
        self.assertEqual(self.ids("هذا هو بالذي قلته"), ["formal-particles"])
        self.assertEqual(self.ids("الرجل الذى جاء"), ["formal-particles"])
        self.assertEqual(self.ids("تأخرت لأنه تعبان"), ["formal-words"])
        self.assertEqual(self.ids("نروح الان"), ["formal-words"])
        self.assertEqual(self.ids("اين السوق"), ["formal-words"])
        self.assertEqual(self.ids("خصم كبير ٪"), ["tts-symbols"])
        self.assertEqual(self.ids("مرت ثلاثة أشهر"), ["formal-plurals"])

    def test_decomposed_hamza_is_normalised(self):
        found = lahja.lint_text("وين أين السوق", self.pack, None,
                                lahja.effective_scope(self.pack, "all"))[0]
        self.assertEqual([f["rule"] for f in found], ["saudi/formal-words"])
        self.assertEqual(found[0]["col"], 5)
        self.assertEqual(found[0]["text"], "أين")

    def test_false_positives_are_gone(self):
        for text in ("شكراً لك", "شكرًا لك", "طبعاً نروح", "مو دايمًا", "هذا أشهر مطعم في الحي",
                     "كتبوا «30» بالحروف الكبيرة"):
            self.assertEqual(self.ids(text), [], text)
        self.assertEqual(self.ids("عندي 30"), ["numerals"])
        self.assertEqual(self.ids("غالباً"), ["tanween"])


if __name__ == "__main__":
    unittest.main()
