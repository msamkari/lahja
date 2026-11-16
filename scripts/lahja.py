#!/usr/bin/env python3
"""lahja: a deterministic linter for spoken-word scripts, driven by dialect packs.

Usage: python lahja.py [--pack NAME] [--pack-dir DIR] [--format text|json]
                       [--all-lines | --tagged-only] -- FILE [FILE ...]
       python lahja.py --list-packs [--pack-dir DIR]

Read-only: it never writes a file and never opens a network connection.
Standard library only, Python 3.8+.
Exit codes: 0 clean (or warnings only), 2 findings with severity "error", 1 tool error.
"""
import argparse
import faulthandler
import functools
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import threading
import unicodedata

VERSION = "0.1.2"

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PACK_BYTES = 256 * 1024
MAX_FILES = 500
MAX_PACKS_LISTED = 200
MAX_RULES = 200
MAX_WORDS = 500
MAX_WORD_LEN = 80
MAX_PATTERN_LEN = 300
MAX_TEXT_FIELD = 300
MAX_LINE_CHARS = 2000
MAX_UNBOUNDED = 4      # wide repeats (* + {n,} or a bounded repeat above WIDE_REPEAT) per pattern
MAX_ADJACENT = 2       # wide repeats directly next to each other, like .*.*.*
MAX_OPTIONAL = 6       # optional parts (? or {0,1}) per pattern
WIDE_REPEAT = 10
MAX_REPEAT = 1000
MAX_FINDINGS_PER_FILE = 500
SNIPPET_CHARS = 60
DEFAULT_TIMEOUT = 30.0
WORKER_GRACE = 3.0         # the worker's own deadline is the timeout plus this many seconds
MAX_PAYLOAD = 4 * 1024 * 1024
TOKEN_ENV = "LAHJA_WORKER_TOKEN"

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
RESERVED_NAMES = frozenset(["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
                           + ["COM%d" % i for i in range(1, 10)] + ["LPT%d" % i for i in range(1, 10)])
# Invisible characters an editor may put in front of a tag: RLM, LRM, ZWSP, BOM, NBSP, bidi controls.
LEADING_INVISIBLE_RE = re.compile(r"[\s\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\u061c\u00ad\ufeff]+")
BRACE_RE = re.compile(r"\{(\d*)(,)?(\d*)\}")
INLINE_VERBOSE_RE = re.compile(r"\(\?[a-zA-Z]*-?[a-zA-Z]*x")
NEWLINE_RE = re.compile(r"\r\n|\n|\r")

# Arabic combining marks (harakat, tanween, shadda, sukun, dagger alef) and tatweel.
MARKS = "\u064B-\u065F\u0670\u0640"
MARKS_RE = re.compile("[" + MARKS + "]")
DIGITS = "0-9\u0660-\u0669\u06F0-\u06F9"
NUMERAL_RE = re.compile("[" + DIGITS + "]+(?:[.,\u066B\u066C:/][" + DIGITS + "]+)*")
LATIN = "A-Za-z\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u024F"
LATIN_RE = re.compile("[" + LATIN + "]+(?:['\u2019-][" + LATIN + "]+)*")

KINDS = ("regex", "wordlist", "numeral", "latin", "length")
SEVERITIES = ("error", "warn")
COMMON_KEYS = {"id", "kind", "severity", "message", "fix", "allow", "ignore_inside"}
KIND_KEYS = {
    "regex": {"pattern", "ignore_case"},
    "wordlist": {"words", "prefixes"},
    "numeral": set(),
    "latin": set(),
    "length": {"max_chars", "max_words", "sentence_end"},
}
PACK_KEYS = {"id", "label", "description", "line_filter", "scope", "rules"}
SCOPE_MODES = ("all", "tagged")
SCOPE_KEYS = {"mode", "tag_pattern"}
DEFAULT_TAG_PATTERN = r"^\s*\[[^\]]+\]"


class PackError(Exception):
    """A pack file is missing, unreadable or does not follow the schema."""


class ToolError(Exception):
    """A problem with the command line or an input file."""


# ---------------------------------------------------------------- output safety

def sanitize(value, limit=None):
    """Make text safe to print: drop control, format (bidi) and unassigned characters."""
    out = []
    for ch in str(value):
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Zl", "Zp"):
            out.append(" ")
        elif cat in ("Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    text = "".join(out)
    if limit is not None and len(text) > limit:
        text = text[:limit] + "\u2026"
    return text


def show_path(path):
    return sanitize(str(path).replace("\\", "/"), 200)


# ---------------------------------------------------------------- regex guard

def _quantifier_at(pat, j):
    """Return (length, max) for a quantifier at j (max None = unbounded), else None."""
    if j >= len(pat):
        return None
    c = pat[j]
    if c in "*+":
        return (1, None)
    if c == "?":
        return (1, 1)
    if c == "{":
        m = BRACE_RE.match(pat, j)
        if m:
            lo, comma, hi = m.group(1), m.group(2), m.group(3)
            if comma is None:
                if lo == "":
                    return None
                return (m.end() - j, int(lo))
            if hi == "":
                return (m.end() - j, None)
            return (m.end() - j, int(hi))
    return None


def regex_problem(pat):
    """Structural complexity guard. Returns a reason string, or None when the pattern is accepted.

    This is a HEURISTIC, not a proof. The standard library has no regex timeout, so patterns that
    can backtrack badly are refused up front: nested repeats, alternation inside a repeated group,
    backreferences, conditionals, very large repeat counts, too many wide repeats (* + {n,} or a
    large {n,m}) or too many of them in a row (.*.*.*), too many optional parts. Some slow
    patterns still pass; the real guard is the time limit on the worker process (--timeout).
    """
    if not isinstance(pat, str) or pat == "":
        return "must be a non-empty string"
    if len(pat) > MAX_PATTERN_LEN:
        return "is longer than %d characters" % MAX_PATTERN_LEN
    n = len(pat)
    stack = [False]  # per open group: does it contain a repeat or an alternation?
    state = {"wide": 0, "optional": 0, "run": 0}

    def apply_quantifier(i, inner_complex):
        q = _quantifier_at(pat, i)
        if q is None:
            state["run"] = 0
            return i, None
        length, mx = q
        repeats = mx is None or mx > 1
        if mx is not None and mx > MAX_REPEAT:
            return i, "uses a repeat count above %d" % MAX_REPEAT
        if repeats and inner_complex:
            return i, ("repeats a group that itself contains a repeat, an optional part or an alternation "
                       "(this can take exponential time)")
        if mx == 1:
            state["optional"] += 1
            if state["optional"] > MAX_OPTIONAL:
                return i, "uses more than %d optional parts (? or {0,1})" % MAX_OPTIONAL
        if mx is None or mx > WIDE_REPEAT:
            state["wide"] += 1
            state["run"] += 1
            if state["wide"] > MAX_UNBOUNDED:
                return i, ("uses more than %d unbounded or large repeats (* + {n,} {n,m})"
                           % MAX_UNBOUNDED)
            if state["run"] > MAX_ADJACENT:
                return i, ("has more than %d unbounded or large repeats in a row (like .*.*.*)"
                           % MAX_ADJACENT)
        else:
            state["run"] = 0
        if repeats or mx == 1:
            stack[-1] = True
        i += length
        if i < n and pat[i] in "?+":
            i += 1
        return i, None

    i = 0
    while i < n:
        c = pat[i]
        if c == "\\":
            if i + 1 >= n:
                return "ends with a lone backslash"
            if pat[i + 1] in "123456789":
                return "uses a backreference (not allowed)"
            i, problem = apply_quantifier(i + 2, False)
        elif c == "[":
            j = i + 1
            if j < n and pat[j] == "^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 2 if pat[j] == "\\" else 1
            if j >= n:
                return "has an unterminated character class"
            i, problem = apply_quantifier(j + 1, False)
        elif c == "(":
            if pat.startswith("(?P=", i):
                return "uses a backreference (not allowed)"
            if pat.startswith("(?(", i):
                return "uses a conditional group (not allowed)"
            if INLINE_VERBOSE_RE.match(pat, i):
                return "uses the verbose flag x (not allowed)"
            stack.append(False)
            state["run"] = 0
            i += 1
            problem = None
        elif c == ")":
            if len(stack) == 1:
                return "has an unbalanced parenthesis"
            inner = stack.pop()
            i, problem = apply_quantifier(i + 1, inner)
            if inner:
                stack[-1] = True
        elif c == "|":
            stack[-1] = True
            state["run"] = 0
            i += 1
            problem = None
        else:
            i, problem = apply_quantifier(i + 1, False)
        if problem:
            return problem
    if len(stack) != 1:
        return "has an unbalanced parenthesis"
    return None


def compile_guarded(pat, what, flags=0, allow_empty=False):
    problem = regex_problem(pat)
    if problem:
        raise PackError("%s: the regex %s" % (what, problem))
    try:
        compiled = re.compile(pat, flags)
    except (re.error, RecursionError, OverflowError, ValueError) as exc:
        raise PackError("%s: invalid regex (%s)" % (what, sanitize(exc, 120)))
    if not allow_empty and compiled.search("") is not None:
        raise PackError("%s: the regex matches empty text; make it match at least one character" % what)
    return compiled


# ---------------------------------------------------------------- pack loading

def _text_field(obj, key, what, required=True, limit=MAX_TEXT_FIELD):
    if key not in obj:
        if required:
            raise PackError("%s: missing \"%s\"" % (what, key))
        return None
    value = obj[key]
    if not isinstance(value, str) or not value.strip():
        raise PackError("%s: \"%s\" must be a non-empty string" % (what, key))
    if len(value) > limit:
        raise PackError("%s: \"%s\" is longer than %d characters" % (what, key, limit))
    return sanitize(value)


def _string_list(obj, key, what, max_items, max_len):
    if key not in obj:
        return []
    value = obj[key]
    if not isinstance(value, list) or len(value) > max_items:
        raise PackError("%s: \"%s\" must be a list of at most %d strings" % (what, key, max_items))
    for item in value:
        if not isinstance(item, str) or item == "" or len(item) > max_len:
            raise PackError("%s: every \"%s\" entry must be a string of 1-%d characters"
                            % (what, key, max_len))
    return list(value)


def _positive_int(obj, key, what):
    if key not in obj:
        return None
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 100000:
        raise PackError("%s: \"%s\" must be a whole number from 1 to 100000" % (what, key))
    return value


COMBINERS = "\u0653\u0654\u0655"   # madda / hamza above / hamza below: they compose with alef, waw, yeh
TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]")


def _strip_marks(text):
    return MARKS_RE.sub("", text)


@functools.lru_cache(maxsize=512)
def _prep(text):
    """Normalise text for word matching and cut it into tokens, keeping a map back to the original.

    Marks and tatweel are dropped, a base letter plus a combining hamza/madda is composed (so text
    saved in decomposed form still matches), letters are lower-cased one by one (so the length never
    changes). Returns (norm, starts, ends, tokens, token_starts, token_ends); starts/ends map each
    normalised character to its span in the original text, tokens are runs of word characters, single other
    characters, or " " for a whitespace run.
    """
    out, starts, ends = [], [], []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if MARKS_RE.match(ch):
            i += 1
            continue
        j = i + 1
        if j < n and text[j] in COMBINERS:
            composed = unicodedata.normalize("NFC", ch + text[j])
            if len(composed) == 1:
                ch = composed
                j += 1
        low = ch.lower()
        out.append(low if len(low) == 1 else ch)
        starts.append(i)
        ends.append(j)
        i = j
    norm = "".join(out)
    tokens, tstarts, tends = [], [], []
    for m in TOKEN_RE.finditer(norm):
        tok = m.group()
        tokens.append(" " if tok.isspace() else tok)
        tstarts.append(m.start())
        tends.append(m.end())
    return norm, tuple(starts), tuple(ends), tuple(tokens), tuple(tstarts), tuple(tends)


def _word_tokens(word):
    return _prep(" ".join(word.split()))[3]


def _is_word_char(ch):
    return ch.isalnum() or ch == "_"


def _build_wordlist(rule, what):
    words = rule.get("words")
    if not isinstance(words, list) or not words:
        raise PackError("%s: \"words\" must be a non-empty list" % what)
    if len(words) > MAX_WORDS:
        raise PackError("%s: \"words\" has more than %d entries" % (what, MAX_WORDS))
    table = {}
    for entry in words:
        fix = None
        if isinstance(entry, dict):
            extra = set(entry) - {"word", "fix"}
            if extra:
                raise PackError("%s: unknown key in a words entry: %s"
                                % (what, sanitize(sorted(extra)[0], 40)))
            word = entry.get("word")
            if "fix" in entry:
                fix = _text_field(entry, "fix", what)
        else:
            word = entry
        if not isinstance(word, str):
            raise PackError("%s: every word must be a string or {\"word\": ..., \"fix\": ...}" % what)
        key = _word_tokens(word)
        if not key or len(word) > MAX_WORD_LEN:
            raise PackError("%s: every word must have 1-%d characters" % (what, MAX_WORD_LEN))
        table[key] = fix
    prefixes = []
    for item in _string_list(rule, "prefixes", what, 10, 8):
        norm = _prep(item.strip())[0]
        if norm and not any(c.isspace() for c in norm) and norm not in prefixes:
            prefixes.append(norm)
    # One or two stacked prefixes (waw + alef-lam); the shortest strip is tried first.
    combos = list(prefixes) + [a + b for a in prefixes for b in prefixes]
    combos = sorted(set(combos), key=len)
    first_tokens = set(key[0] for key in table)
    lengths = sorted(set(len(key) for key in table), reverse=True)

    def find(toks, i):
        """The longest word at token i, or None. Returns (key, token_count)."""
        tok = toks[i]
        candidates = [(tok, 0)] if tok in first_tokens else []
        if combos and _is_word_char(tok[0]):
            for combo in combos:
                if len(tok) > len(combo) and tok.startswith(combo) and tok[len(combo):] in first_tokens:
                    candidates.append((tok[len(combo):], 1))
        for first, _stripped in candidates:
            for length in lengths:
                key = (first,) + tuple(toks[i + 1:i + length])
                if len(key) == length and key in table:
                    return key, length
        return None

    def matcher(text):
        norm, starts, ends, toks, tstarts, tends = _prep(text)
        count = len(toks)
        i = 0
        while i < count:
            if toks[i] == " ":
                i += 1
                continue
            hit = find(toks, i)
            if hit is None:
                i += 1
                continue
            key, length = hit
            first = tstarts[i]
            last = tends[i + length - 1]
            before = norm[first - 1] if first > 0 else " "
            after = norm[last] if last < len(norm) else " "
            if _is_word_char(before) or _is_word_char(after):
                i += 1
                continue
            end = ends[last - 1]
            while end < len(text) and MARKS_RE.match(text[end]):
                end += 1
            yield starts[first], end, table[key], None
            i += length
    return matcher


def _build_regex(rule, what):
    if "ignore_case" in rule and not isinstance(rule["ignore_case"], bool):
        raise PackError("%s: \"ignore_case\" must be true or false" % what)
    if "pattern" not in rule:
        raise PackError("%s: missing \"pattern\"" % what)
    flags = re.IGNORECASE if rule.get("ignore_case") else 0
    compiled = compile_guarded(rule["pattern"], what, flags)

    def matcher(text):
        for m in compiled.finditer(text):
            if m.end() > m.start():
                yield m.start(), m.end(), None, None
    return matcher


def _simple_matcher(compiled):
    def matcher(text):
        for m in compiled.finditer(text):
            yield m.start(), m.end(), None, None
    return matcher


def _build_length(rule, what):
    max_chars = _positive_int(rule, "max_chars", what)
    max_words = _positive_int(rule, "max_words", what)
    if max_chars is None and max_words is None:
        raise PackError("%s: a length rule needs \"max_chars\" or \"max_words\"" % what)
    ends = ".!?\u061F\u2026"
    if "sentence_end" in rule:
        ends = rule["sentence_end"]
        if not isinstance(ends, str) or not 1 <= len(ends) <= 12:
            raise PackError("%s: \"sentence_end\" must be a string of 1-12 characters" % what)
    segment = re.compile("[^" + "".join(re.escape(ch) for ch in ends) + "]+")

    def matcher(text):
        for m in segment.finditer(text):
            raw = m.group(0)
            body = raw.strip()
            if not body:
                continue
            start = m.start() + (len(raw) - len(raw.lstrip()))
            chars = len(body)
            words = len(body.split())
            over = []
            if max_chars is not None and chars > max_chars:
                over.append("%d characters, max %d" % (chars, max_chars))
            if max_words is not None and words > max_words:
                over.append("%d words, max %d" % (words, max_words))
            if over:
                yield start, start + chars, None, "; ".join(over)
    return matcher


def _build_rule(rule, index, seen):
    what = "rule #%d" % (index + 1)
    if not isinstance(rule, dict):
        raise PackError("%s: must be an object" % what)
    rule_id = rule.get("id")
    if not isinstance(rule_id, str) or not NAME_RE.match(rule_id):  # NAME_RE ends with \Z
        raise PackError("%s: \"id\" must be lowercase letters, digits, - or _ (max 64)" % what)
    what = "rule \"%s\"" % rule_id
    if rule_id in seen:
        raise PackError("%s: duplicate rule id" % what)
    seen.add(rule_id)
    kind = rule.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise PackError("%s: \"kind\" must be one of %s" % (what, ", ".join(KINDS)))
    severity = rule.get("severity")
    if not isinstance(severity, str) or severity not in SEVERITIES:
        raise PackError("%s: \"severity\" must be \"error\" or \"warn\"" % what)
    for key in rule:
        if isinstance(key, str) and key.startswith("_"):
            continue
        if key not in COMMON_KEYS and key not in KIND_KEYS[kind]:
            raise PackError("%s: unknown key \"%s\" for kind \"%s\"" % (what, sanitize(key, 40), kind))
    message = _text_field(rule, "message", what)
    fix = _text_field(rule, "fix", what, required=False)
    allow = _string_list(rule, "allow", what, 100, MAX_WORD_LEN)
    pairs = []
    raw_pairs = rule.get("ignore_inside", [])
    if not isinstance(raw_pairs, list) or len(raw_pairs) > 10:
        raise PackError("%s: \"ignore_inside\" must be a list of at most 10 [open, close] pairs" % what)
    for pair in raw_pairs:
        ok = (isinstance(pair, list) and len(pair) == 2
              and all(isinstance(p, str) and 1 <= len(p) <= 4 for p in pair))
        if not ok:
            raise PackError("%s: every \"ignore_inside\" entry must be [open, close], 1-4 characters each" % what)
        pairs.append((pair[0], pair[1]))
    if kind == "regex":
        matcher = _build_regex(rule, what)
    elif kind == "wordlist":
        matcher = _build_wordlist(rule, what)
    elif kind == "numeral":
        matcher = _simple_matcher(NUMERAL_RE)
    elif kind == "latin":
        matcher = _simple_matcher(LATIN_RE)
    else:
        matcher = _build_length(rule, what)
    return {"id": rule_id, "kind": kind, "severity": severity, "message": message, "fix": fix,
            "allow": allow, "ignore_inside": pairs, "matcher": matcher}


def _build_scope(raw):
    """Validate the optional "scope" object. Returns {"mode", "tag_pattern", "tag_regex"}."""
    if raw is None:
        return {"mode": "all", "tag_pattern": None, "tag_regex": None}
    if not isinstance(raw, dict):
        raise PackError("\"scope\" must be an object like {\"mode\": \"tagged\", \"tag_pattern\": \"...\"}")
    for key in raw:
        if isinstance(key, str) and key.startswith("_"):
            continue
        if key not in SCOPE_KEYS:
            raise PackError("scope: unknown key \"%s\"" % sanitize(key, 40))
    mode = raw.get("mode")
    if not isinstance(mode, str) or mode not in SCOPE_MODES:
        raise PackError("scope: \"mode\" must be one of %s" % ", ".join(SCOPE_MODES))
    pattern = raw.get("tag_pattern")
    if pattern is None:
        if "tag_pattern" in raw:
            raise PackError("scope: \"tag_pattern\" must be a regex string")
        if mode == "tagged":
            raise PackError("scope: mode \"tagged\" needs \"tag_pattern\" (a regex string)")
        return {"mode": mode, "tag_pattern": None, "tag_regex": None}
    if not isinstance(pattern, str):
        raise PackError("scope: \"tag_pattern\" must be a regex string")
    return {"mode": mode, "tag_pattern": pattern,
            "tag_regex": compile_guarded(pattern, "scope tag_pattern")}


def effective_scope(pack, force=None):
    """The scope used for a run. force is None, "all" or "tagged" (from the command line)."""
    scope = pack["scope"]
    mode = force or scope["mode"]
    if mode == "all":
        return {"mode": "all", "tag_pattern": scope["tag_pattern"], "tag_regex": None}
    if scope["tag_regex"] is not None:
        return {"mode": "tagged", "tag_pattern": scope["tag_pattern"], "tag_regex": scope["tag_regex"]}
    return {"mode": "tagged", "tag_pattern": DEFAULT_TAG_PATTERN,
            "tag_regex": compile_guarded(DEFAULT_TAG_PATTERN, "default tag pattern")}


def scope_info(scope):
    pattern = scope["tag_pattern"] if scope["mode"] == "tagged" else None
    return {"mode": scope["mode"],
            "tag_pattern": sanitize(pattern, 300) if pattern is not None else None}


def valid_pack_name(name):
    """A pack id / pack file stem: lowercase letters, digits, - and _, not a Windows device name."""
    return (isinstance(name, str) and NAME_RE.match(name) is not None
            and name.split(".")[0].upper() not in RESERVED_NAMES)


def validate_pack(data, expected_id=None):
    """Check a decoded pack against the schema and compile it. Raises PackError."""
    if not isinstance(data, dict):
        raise PackError("the pack must be a JSON object")
    for key in data:
        if isinstance(key, str) and key.startswith("_"):
            continue
        if key not in PACK_KEYS:
            raise PackError("unknown top-level key \"%s\"" % sanitize(key, 40))
    pack_id = data.get("id")
    if not valid_pack_name(pack_id):
        raise PackError("\"id\" must be lowercase letters, digits, - or _ (max 64) "
                        "and not a reserved name like con, nul, aux")
    if expected_id is not None and pack_id != expected_id:
        raise PackError("\"id\" is \"%s\" but the file name says \"%s\"; they must match"
                        % (pack_id, sanitize(expected_id, 64)))
    label = _text_field(data, "label", "pack", limit=100)
    description = _text_field(data, "description", "pack", required=False)
    line_filter = None
    if "line_filter" in data:
        if not isinstance(data["line_filter"], str):
            raise PackError("\"line_filter\" must be a regex string")
        line_filter = compile_guarded(data["line_filter"], "line_filter")
    scope = _build_scope(data["scope"] if "scope" in data else None)
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise PackError("\"rules\" must be a non-empty list")
    if len(rules) > MAX_RULES:
        raise PackError("\"rules\" has more than %d entries" % MAX_RULES)
    seen = set()
    compiled = [_build_rule(rule, i, seen) for i, rule in enumerate(rules)]
    return {"id": pack_id, "label": label, "description": description,
            "line_filter": line_filter, "scope": scope, "rules": compiled}


def load_pack_file(path, expected_id=None):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_PACK_BYTES + 1)
    except OSError as exc:
        raise PackError("cannot read the pack file (%s)" % sanitize(exc.strerror or exc, 80))
    if len(raw) > MAX_PACK_BYTES:
        raise PackError("the pack file is larger than %d bytes" % MAX_PACK_BYTES)
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise PackError("the pack file is not valid UTF-8")
    except ValueError as exc:
        raise PackError("the pack file is not valid JSON (%s)" % sanitize(exc, 120))
    except RecursionError:
        raise PackError("the pack file is nested too deeply")
    return validate_pack(data, expected_id)


def builtin_pack_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packs")


def find_pack(name, pack_dir=None):
    if not valid_pack_name(name):
        raise PackError("pack name must be lowercase letters, digits, - or _ (no paths, no reserved names)")
    folders = ([pack_dir] if pack_dir else []) + [builtin_pack_dir()]
    for folder in folders:
        path = os.path.join(folder, name + ".json")
        # Compare the exact file name, so a case-insensitive file system (Windows, macOS) does not
        # load Saudi.JSON for "saudi"; --list-packs applies the same rule.
        if _listing_has(folder, name + ".json") and os.path.isfile(path):
            try:
                return load_pack_file(path, name)
            except PackError as exc:
                raise PackError("pack \"%s\" (%s): %s" % (name, show_path(path), exc))
    raise PackError("pack \"%s\" not found; use --list-packs to see what is available" % name)


def _listing_has(folder, file_name):
    try:
        return file_name in os.listdir(folder)
    except OSError:
        return False


def list_packs(pack_dir=None, info=None):
    """Return a list of {id, source, valid, label, rules, error} for every pack file found.

    info, when given, receives "skipped": how many pack files were not listed because of the cap.
    """
    rows = []
    seen = set()
    skipped = 0
    sources = ([("custom", pack_dir)] if pack_dir else []) + [("builtin", builtin_pack_dir())]
    for source, folder in sources:
        try:
            names = sorted(n for n in os.listdir(folder) if n.lower().endswith(".json"))
        except OSError as exc:
            raise ToolError("cannot read the pack folder %s (%s)"
                            % (show_path(folder), sanitize(exc.strerror or exc, 80)))
        skipped += max(0, len(names) - MAX_PACKS_LISTED)
        for file_name in names[:MAX_PACKS_LISTED]:
            stem = file_name[:-5]
            row = {"id": sanitize(stem, 64), "source": source, "valid": False,
                   "label": None, "rules": 0, "scope": None, "error": None, "shadowed": stem in seen}
            try:
                if file_name != stem + ".json" or not valid_pack_name(stem):
                    raise PackError("the file name must be <id>.json with a lowercase id "
                                    "(letters, digits, - and _), so --pack can load it")
                pack = load_pack_file(os.path.join(folder, file_name), stem)
                row.update(valid=True, label=pack["label"], rules=len(pack["rules"]),
                           scope=scope_info(effective_scope(pack)))
            except PackError as exc:
                row["error"] = sanitize(exc, 500)
            seen.add(stem)
            rows.append(row)
    if info is not None:
        info["skipped"] = skipped
    return rows


# ---------------------------------------------------------------- linting

def _mask(text, pairs):
    """Blank out text inside each [open, close] pair, keeping every column in place."""
    for opener, closer in pairs:
        pos = 0
        while True:
            start = text.find(opener, pos)
            if start < 0:
                break
            end = text.find(closer, start + len(opener))
            if end < 0:
                break
            end += len(closer)
            text = text[:start] + " " * (end - start) + text[end:]
            pos = end
    return text


def _allowed_spans(text, allow):
    spans = []
    for item in allow:
        pos = text.find(item)
        while pos >= 0:
            spans.append((pos, pos + len(item)))
            pos = text.find(item, pos + 1)
    return spans


def _snippet(segment, start, end):
    """The matched text; a match made only of combining marks is shown with its whole word."""
    if all(unicodedata.category(ch) == "Mn" for ch in segment[start:end]):
        while start > 0 and not segment[start - 1].isspace():
            start -= 1
        while end < len(segment) and not segment[end].isspace():
            end += 1
    return sanitize(segment[start:end], SNIPPET_CHARS)


def _tag_end(tag_regex, probe):
    """Column where the tag ends, or None. Invisible characters (RLM, ZWSP, BOM, NBSP...) in front of the tag are tolerated."""
    m = tag_regex.search(probe)
    if m is not None:
        return m.end()
    lead = LEADING_INVISIBLE_RE.match(probe)
    if lead is not None:
        m = tag_regex.search(probe[lead.end():])
        if m is not None:
            return lead.end() + m.end()
    return None


def lint_text(text, pack, line_filter=None, scope=None, progress=None, stats=None):
    """Lint decoded text. Returns (findings, lines_checked). Columns are 1-based code points.

    scope is a dict from effective_scope(); None means the pack's own scope. In tagged mode only
    lines where the tag regex matches are linted, and the matched tag itself is hidden from the rules.
    progress, when given, is called with each rule id before that rule runs over the file (the worker
    uses it to say which rule was running if a time limit hits). stats, when given, receives
    "skipped_untagged": the number of non-empty lines skipped because they have no tag.

    The line filter and the scope are applied once, then each rule runs over the kept lines.
    """
    checked = 0
    skipped = 0
    active_filter = line_filter if line_filter is not None else pack["line_filter"]
    pack_id = pack["id"]
    tag_regex = (scope if scope is not None else effective_scope(pack))["tag_regex"]
    items = []
    findings = []
    for number, line in enumerate(NEWLINE_RE.split(text), 1):
        too_long = len(line) > MAX_LINE_CHARS
        probe = line[:MAX_LINE_CHARS] if too_long else line
        start, end = 0, len(line)
        if active_filter is not None:
            m = active_filter.search(probe)
            if m is None:
                continue
            if active_filter.groups >= 1 and m.group(1) is not None:
                start, end = m.start(1), m.end(1)
        if tag_regex is not None:
            tag_end = _tag_end(tag_regex, probe)
            if tag_end is None:
                if line.strip():
                    skipped += 1
                continue
            start = max(start, tag_end)
            end = max(end, start)
        segment = line[start:end]
        if not segment.strip():
            continue
        checked += 1
        if too_long:
            findings.append({"line": number, "col": 1, "rule": "lahja/long-line", "severity": "warn",
                             "message": "line has more than %d characters and was not checked; "
                                        "break it into shorter lines" % MAX_LINE_CHARS,
                             "text": sanitize(line[:SNIPPET_CHARS], SNIPPET_CHARS), "fix": None})
            continue
        items.append((number, start, segment))
    if stats is not None:
        stats["skipped_untagged"] = skipped
    for rule in pack["rules"]:
        if progress is not None:
            progress(rule["id"])
        got = 0
        for number, offset, segment in items:
            target = _mask(segment, rule["ignore_inside"]) if rule["ignore_inside"] else segment
            allowed = _allowed_spans(segment, rule["allow"]) if rule["allow"] else []
            for start, end, word_fix, detail in rule["matcher"](target):
                if any(a <= start and end <= b for a, b in allowed):
                    continue
                message = rule["message"]
                if detail:
                    message = "%s (%s)" % (message, detail)
                findings.append({"line": number, "col": offset + start + 1,
                                 "rule": pack_id + "/" + rule["id"], "severity": rule["severity"],
                                 "message": message,
                                 "text": _snippet(segment, start, end),
                                 "fix": word_fix or rule["fix"]})
                got += 1
                if got >= MAX_FINDINGS_PER_FILE:
                    break
            if got >= MAX_FINDINGS_PER_FILE:
                break
    # Each rule keeps at most its first MAX findings, so the first MAX of all of them are all here.
    findings.sort(key=lambda f: (f["line"], f["col"]))
    if len(findings) >= MAX_FINDINGS_PER_FILE:
        findings = findings[:MAX_FINDINGS_PER_FILE]
        findings.append({"line": findings[-1]["line"], "col": 1, "rule": "lahja/too-many-findings",
                         "severity": "warn",
                         "message": "stopped after %d findings in this file" % MAX_FINDINGS_PER_FILE,
                         "text": "", "fix": None})
    return findings, checked


def is_device_name(path):
    """True for Windows device names (CON, NUL, AUX, COM1, CON.txt, \\\\.\\pipe\\x ...): reading one can block."""
    norm = str(path).replace("\\", "/")
    if norm.startswith("//./"):
        return True
    base = norm.rsplit("/", 1)[-1]
    return base.split(".")[0].rstrip(" ").upper() in RESERVED_NAMES


def read_script(path):
    """Read a script file as text (UTF-8, BOM tolerated). Raises ToolError."""
    if is_device_name(path):
        raise ToolError("is a device name (CON, NUL, COM1 ...), not a script file")
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        raise ToolError("cannot read the file (%s)" % sanitize(exc.strerror or exc, 80))
    if stat.S_ISDIR(mode):
        raise ToolError("is a folder, not a file")
    if not stat.S_ISREG(mode):
        raise ToolError("is not a regular file (device or pipe)")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise ToolError("cannot read the file (%s)" % sanitize(exc.strerror or exc, 80))
    if len(raw) > MAX_FILE_BYTES:
        raise ToolError("is larger than %d bytes; split it" % MAX_FILE_BYTES)
    if b"\x00" in raw:
        raise ToolError("looks like a binary file (or UTF-16); save it as UTF-8 text")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ToolError("is not valid UTF-8 text")


# ---------------------------------------------------------------- command line

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write("lahja: error: %s\n" % sanitize(message, 300))
        sys.exit(1)


def build_parser():
    parser = _Parser(
        prog="lahja", allow_abbrev=False,
        description="Lint spoken-word scripts against a dialect pack. Read-only. "
                    "Exit codes: 0 clean, 2 error findings, 1 tool error.")
    parser.add_argument("files", nargs="*", metavar="FILE",
                        help="script files (UTF-8 text). Put -- before names that start with a dash")
    parser.add_argument("--pack", default="saudi", metavar="NAME", help="pack id (default: saudi)")
    parser.add_argument("--pack-dir", metavar="DIR",
                        help="folder with your own <name>.json packs (searched before the built-in ones)")
    parser.add_argument("--list-packs", action="store_true", help="list and validate available packs")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--line-filter", metavar="REGEX",
                        help="lint only lines matching REGEX; if it has a group, only group 1 is linted")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all-lines", action="store_true",
                       help="lint every non-empty line, ignoring the pack's scope")
    group.add_argument("--tagged-only", action="store_true",
                       help="lint only lines that start with a [tag], even if the pack says all lines")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS",
                        help="hard limit for the whole run (default: %d)" % DEFAULT_TIMEOUT)
    parser.add_argument("--version", action="version", version="lahja " + VERSION)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def _setup_streams():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _fail(message):
    sys.stderr.write("lahja: error: %s\n" % message)


# ---------------------------------------------------------------- worker process
#
# The real work runs in a child process ("worker") so that a slow regex can be stopped, and so that
# the results of the files already linted survive a time limit. The worker prints one JSON record
# per line (ASCII only) and flushes after each; the parent turns the records into the final output.
# The worker also arms its own deadline (faulthandler runs in a C thread, so it fires even while a
# regex holds the interpreter), which ends it without help if the parent is gone.

_WATCH_FILE = []


def _arm_watchdog(seconds):
    """End this process with exit code 1 after `seconds`, even inside a long regex match."""
    try:
        if not _WATCH_FILE:
            _WATCH_FILE.append(open(os.devnull, "w"))
        faulthandler.dump_traceback_later(seconds, repeat=False, file=_WATCH_FILE[0], exit=True)
    except (OSError, ValueError, RuntimeError, AttributeError, OverflowError) as exc:
        raise ToolError("cannot start the time limit (%s); refusing to run without one"
                        % sanitize(exc, 80))


def _emit(record):
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def _worker_list(args):
    info = {}
    rows = list_packs(args.pack_dir, info)
    _emit({"t": "packs", "rows": rows, "skipped": info.get("skipped", 0)})
    _emit({"t": "end"})
    return 0


def _worker_lint(args):
    if not args.files:
        raise ToolError("no files given (see --help)")
    if len(args.files) > MAX_FILES:
        raise ToolError("more than %d files in one run" % MAX_FILES)
    pack = find_pack(args.pack, args.pack_dir)
    cli_filter = None
    if args.line_filter is not None:
        cli_filter = compile_guarded(args.line_filter, "--line-filter")
    force = "all" if args.all_lines else ("tagged" if args.tagged_only else None)
    scope = effective_scope(pack, force)
    _emit({"t": "header", "pack": pack["id"], "scope": scope_info(scope), "files": len(args.files)})
    for path in args.files:
        shown = show_path(path)
        _emit({"t": "stage", "file": shown, "rule": None})
        try:
            text = read_script(path)
        except ToolError as exc:
            _emit({"t": "file", "file": shown, "tool_error": str(exc)})
            continue
        stats = {}

        def progress(rule_id, shown=shown):
            _emit({"t": "stage", "file": shown, "rule": pack["id"] + "/" + rule_id})

        findings, checked = lint_text(text, pack, cli_filter, scope, progress, stats)
        _emit({"t": "file", "file": shown, "findings": findings, "checked": checked,
               "skipped": stats.get("skipped_untagged", 0)})
    _emit({"t": "end"})
    return 0


def _worker(args):
    try:
        if args.pack_dir is not None and not os.path.isdir(args.pack_dir):
            raise ToolError("--pack-dir is not a folder: %s" % show_path(args.pack_dir))
        if args.list_packs:
            return _worker_list(args)
        return _worker_lint(args)
    except (PackError, ToolError) as exc:
        _fail(str(exc))
    except RecursionError:
        _fail("input is nested too deeply")
    except MemoryError:
        _fail("out of memory")
    _emit({"t": "fatal"})
    return 1


def _worker_entry():
    """Entry point of the child process. Refuses to run unless the parent started it."""
    expected = os.environ.get(TOKEN_ENV)
    if not expected:
        _fail("--_worker is internal and cannot be used directly")
        return 1
    try:
        _arm_watchdog(10.0)  # for reading the work order
        raw = sys.stdin.buffer.read(MAX_PAYLOAD + 1)
        if len(raw) > MAX_PAYLOAD:
            raise ValueError("work order too large")
        order = json.loads(raw.decode("utf-8"))
        token = order["token"]
        argv = order["argv"]
        if (not isinstance(token, str) or not isinstance(argv, list)
                or not all(isinstance(a, str) for a in argv)):
            raise ValueError("bad work order")
    except ToolError as exc:
        _fail(str(exc))
        return 1
    except (ValueError, KeyError, TypeError, OSError):
        _fail("--_worker is internal and cannot be used directly")
        return 1
    if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        _fail("--_worker is internal and cannot be used directly")
        return 1
    args = build_parser().parse_args(argv)
    if args._worker or not (1 <= args.timeout <= 600):
        _fail("bad work order")
        return 1
    try:
        _arm_watchdog(args.timeout + WORKER_GRACE)
    except ToolError as exc:
        _fail(str(exc))
        return 1
    return _worker(args)


# ---------------------------------------------------------------- parent process

def _read_events(stream, events):
    try:
        for raw in iter(stream.readline, b""):
            try:
                record = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(record, dict):
                events.append(record)
    except (OSError, ValueError):
        pass


def _supervise(argv, timeout):
    """Run the worker with a hard time limit. Returns (events, returncode, timed_out)."""
    if not sys.executable:
        raise ToolError("cannot find the Python program to start the checking process; "
                        "nothing was checked")
    token = os.urandom(16).hex()
    env = dict(os.environ)
    env[TOKEN_ENV] = token
    order = json.dumps({"token": token, "argv": argv}).encode("ascii")
    command = [sys.executable, "-X", "utf8", os.path.abspath(__file__), "--_worker"]
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    except OSError as exc:
        raise ToolError("cannot start the checking process (%s); nothing was checked"
                        % sanitize(exc.strerror or exc, 80))
    events = []
    reader = threading.Thread(target=_read_events, args=(proc.stdout, events))
    reader.daemon = True
    reader.start()
    try:
        proc.stdin.write(order)
        proc.stdin.close()
    except OSError:
        pass
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    reader.join(5)
    return list(events), proc.returncode, timed_out


def _print_packs(args, record):
    rows = record["rows"]
    skipped = record.get("skipped", 0)
    bad = [r for r in rows if not r["valid"]]
    if args.format == "json":
        print(json.dumps({"tool": "lahja", "version": VERSION, "packs": rows, "skipped": skipped},
                         ensure_ascii=False, indent=2))
    else:
        for row in rows:
            note = " (hidden by a custom pack with the same id)" if row["shadowed"] else ""
            if row["valid"]:
                sc = row["scope"]
                shown = "all" if sc["mode"] == "all" else "tagged " + sc["tag_pattern"]
                print("%s\t%s\t%d rules\tscope: %s\t%s%s"
                      % (row["id"], row["source"], row["rules"], sanitize(shown, 100), row["label"], note))
            else:
                print("%s\t%s\tINVALID: %s" % (row["id"], row["source"], row["error"]))
        if skipped:
            print("lahja: %d more pack file(s) not listed (limit %d per folder)"
                  % (skipped, MAX_PACKS_LISTED))
    return 1 if bad else 0


def _interruption(args, header, stage, done, timed_out, returncode):
    if timed_out:
        what = "stopped after %g seconds" % args.timeout
        hint = "a pack regex is too slow for this input, or the input is too large"
    else:
        what = "the checking process stopped unexpectedly (exit code %s)" % returncode
        hint = "no further detail"
    if header is None:
        where = "while loading the pack"
    elif stage is None:
        where = "before the first file"
    elif stage.get("rule"):
        where = "while checking %s with rule %s" % (stage["file"], stage["rule"])
    else:
        where = "while reading %s" % stage["file"]
    total = len(args.files)
    tail = ""
    if header is not None:
        tail = "; %d of %d file(s) were already checked and their findings are kept" % (done, total)
    message = "%s %s (%s)%s" % (what, where, hint, tail)
    return (stage["file"] if stage else "-"), message


def _print_lint(args, events, returncode, timed_out):
    header = stage = None
    results = []
    ended = False
    for record in events:
        kind = record.get("t")
        if kind == "header":
            header = record
        elif kind == "stage":
            stage = record
        elif kind == "file":
            results.append(record)
        elif kind == "end":
            ended = True
    all_findings, tool_errors, notes = [], [], []
    skipped = 0
    for res in results:
        if res.get("tool_error") is not None:
            tool_errors.append({"file": res["file"], "message": res["tool_error"]})
            continue
        if res["checked"] == 0:
            notes.append({"file": res["file"], "message": "no lines were checked (empty file, or nothing "
                                                          "matched the line filter or the scope)"})
        skipped += res.get("skipped", 0)
        for finding in res["findings"]:
            finding["file"] = res["file"]
            all_findings.append(finding)
    if not ended:
        file_name, message = _interruption(args, header, stage, len(results), timed_out, returncode)
        tool_errors.append({"file": file_name, "message": message})
    errors = sum(1 for f in all_findings if f["severity"] == "error")
    warns = len(all_findings) - errors
    skipped_note = "%d untagged line(s) skipped (use --all-lines)" % skipped if skipped else None
    pack_id = header["pack"] if header else sanitize(args.pack, 64)
    if args.format == "json":
        ordered = [{"file": f["file"], "line": f["line"], "col": f["col"], "rule": f["rule"],
                    "severity": f["severity"], "message": f["message"], "text": f["text"],
                    "fix": f["fix"]} for f in all_findings]
        print(json.dumps({"tool": "lahja", "version": VERSION, "pack": pack_id,
                          "scope": header["scope"] if header else None,
                          "untagged_skipped": skipped, "scope_note": skipped_note, "complete": ended,
                          "findings": ordered, "tool_errors": tool_errors, "notes": notes,
                          "summary": {"files": len(args.files), "error": errors, "warn": warns,
                                      "tool_errors": len(tool_errors)}},
                         ensure_ascii=False, indent=2))
    else:
        for f in all_findings:
            line = "%s:%d:%d: %s %s: %s" % (f["file"], f["line"], f["col"], f["severity"],
                                            f["rule"], f["message"])
            if f["text"]:
                line += " | text: \"%s\"" % f["text"]
            if f["fix"]:
                line += " | fix: %s" % f["fix"]
            print(line)
        for note in notes:
            print("%s: note: %s" % (note["file"], note["message"]))
        if skipped_note:
            print("lahja: %s" % skipped_note)
        print("lahja: %d error, %d warn in %d file(s), pack %s"
              % (errors, warns, len(args.files), pack_id))
    for item in tool_errors:
        _fail("%s: %s" % (item["file"], item["message"]) if not item["message"].startswith(
            ("stopped after", "the checking process")) else item["message"])
    if tool_errors:
        return 1
    return 2 if errors else 0


def _run_parent(argv, args):
    events, returncode, timed_out = _supervise(argv, args.timeout)
    if any(r.get("t") == "fatal" for r in events):
        return 1  # the worker already printed the message
    if args.list_packs:
        for record in events:
            if record.get("t") == "packs":
                return _print_packs(args, record)
        _, message = _interruption(args, None, None, 0, timed_out, returncode)
        _fail(message)
        return 1
    return _print_lint(args, events, returncode, timed_out)


def main(argv=None):
    _setup_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if args._worker:
        return _worker_entry()
    if not (1 <= args.timeout <= 600):
        _fail("--timeout must be between 1 and 600 seconds")
        return 1
    try:
        return _run_parent(argv, args)
    except ToolError as exc:
        _fail(str(exc))
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
    except BrokenPipeError:
        sys.exit(1)
