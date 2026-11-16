# lahja — a Claude Code plugin (v0.1.0)

`lahja` (Arabic for "dialect") is a linter for scripts that are meant to be **spoken**: voiceovers, reels, podcasts. Written text that looks fine can sound wrong when read aloud or when a text-to-speech engine reads it: formal words nobody says, digits, stray Latin letters, tanween, doubled punctuation, sentences too long for one breath. `lahja` finds these and tells you the file, line and column. It never rewrites anything.

The engine is generic. What counts as "wrong" lives in a **dialect pack**, a small JSON file. The plugin ships with a Saudi pack; you add your own dialect by writing a pack in your own project, without touching the plugin.

[العربية](README.ar.md)

## Requirements
Python 3.8 or newer. Standard library only: nothing to install. The skills call `python`; where only `python3` exists, ask Claude to run the same command with `python3`.

## Use
Ask Claude: "lint this script with lahja", or "make me a lahja pack for my dialect". Or run it yourself:

```
python scripts/lahja.py [--pack saudi] [--pack-dir DIR] [--format text|json] [--line-filter REGEX] [--timeout SECONDS] -- FILE [FILE ...]
python scripts/lahja.py --list-packs [--pack-dir DIR]
```

Output, one line per finding and a summary:

```
script.txt:2:11: error saudi/numerals: <message> | text: "3" | fix: <hint>
lahja: 1 error, 0 warn in 1 file(s), pack saudi
```

- Lines and columns start at 1. A column counts characters (code points), not bytes; a leading BOM is ignored and CRLF, LF and CR line ends all work.
- Exit codes: `0` clean or warnings only, `2` at least one `error` finding, `1` tool error (missing or unreadable file, invalid pack, bad option, timeout). When a run has both a tool error and findings, the findings are still printed and the exit code is `1`.
- Put `--` before file names so a name that starts with a dash is not read as an option.
- `--line-filter REGEX` lints only the lines that match. If the regex has a group, only group 1 is linted. Example for scripts where spoken lines start with a tag such as `[calm]`: `--line-filter '^\s*\[[a-z ]+\]\s*(.*)$'`. Without a filter every non-empty line is linted, including headings.
- Files must be UTF-8 text of at most 2 MB. Lines longer than 2000 characters are reported and not scanned.

## Dialect packs

A pack is `<id>.json`. Built-in packs are in `packs/`; your own go in any folder you pass with `--pack-dir` (searched first, so a pack of yours with the same id replaces the built-in one for that run). `examples/packs/plain-english.json` is a small pack to copy.

```json
{
  "id": "my-dialect",
  "label": "My dialect",
  "description": "optional",
  "line_filter": "optional regex, same meaning as --line-filter",
  "rules": [
    {"id": "stiff-words", "kind": "wordlist", "severity": "warn",
     "message": "Nobody says this out loud",
     "words": [{"word": "utilize", "fix": "use"}, "henceforth"]}
  ]
}
```

Every rule has:

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | lowercase letters, digits, `-`, `_`; unique in the pack. Findings show `pack/rule`. |
| `kind` | yes | `regex`, `wordlist`, `numeral`, `latin` or `length` |
| `severity` | yes | `error` (exit code 2) or `warn` |
| `message` | yes | what is wrong, up to 300 characters |
| `fix` | no | a hint: what to write instead |
| `allow` | no | list of exact strings; a match inside one of them is skipped |
| `ignore_inside` | no | list of `[open, close]` pairs, e.g. `[["«", "»"]]`; text between them is skipped by this rule |

Per kind:

| Kind | Extra fields | What it flags |
|---|---|---|
| `regex` | `pattern` (required), `ignore_case` | every match of the pattern |
| `wordlist` | `words` (required): strings or `{"word", "fix"}`; `prefixes`: short strings that may be glued to the front of a word (Arabic و, ف, ال) | whole words or phrases. Case is ignored, and so are Arabic diacritics and tatweel, in both the list and the text. A word inside a longer word does not match. |
| `numeral` | none | runs of digits: ASCII, Arabic-Indic and Persian, including forms like `3.75` and `10:30` |
| `latin` | none | runs of Latin letters |
| `length` | `max_words` and/or `max_chars`; `sentence_end` (default `.!?؟…`) | a sentence longer than the limit. Sentences never cross a line. |

Keys that start with `_` are ignored, so you can leave comments. Any other unknown key is an error, to catch typos. Limits: pack file 256 KB, 200 rules, 500 words per rule.

### Regex safety
Python's standard library has no regex timeout, so a pack regex is checked before use and refused when it: repeats a group that itself contains a repeat, an optional part or an alternation (`(a+)+`, `(a|ab)*`, `(a?){30}`), uses the inline verbose flag `x`, uses a backreference or a conditional, is longer than 300 characters, has a repeat count above 1000 or more than six unbounded repeats, or can match empty text. This guard is a heuristic: it refuses some harmless patterns (write `(?:foo|bar)+` as two rules or without the repeat) and cannot catch every slow one. As a second line of defence the real work runs in a child process that is stopped after `--timeout` seconds (default 30); the run then ends with exit code 1.

### The Saudi pack
`packs/saudi.json` targets everyday Saudi "white" dialect (Gulf, Najdi-leaning) for voiceover. Errors: tanween, Latin letters outside `«…»` and `[...]`, digits, and formal particles (ماذا، لماذا، سوف، الذي، التي، الذين، هكذا، لكي). Warnings: formal words with a dialect alternative (الآن ← الحين، يجب ← لازم، أريد ← أبي …), formal plurals, a vowel mark on a final kaf, symbols and punctuation that speech engines misread, tatweel, and sentences over 25 words. It is one speaker's taste, not a standard: copy it under a new id and change it.

## Safety and privacy
- Read-only: the script opens files for reading only and never writes, deletes or renames anything.
- No network, no keys, no environment variables, no telemetry. Your scripts stay on your machine.
- Script text, file names and pack messages are printed with control and bidirectional-override characters removed, and matched text is cut to 60 characters.
- In the skills, Claude is told to treat script text as data, to show findings first, and to edit a file only after you approve. Creating a pack writes only the one file you choose.

## Limits
- It matches text, not grammar: it cannot tell a formal word from the same letters used as a name or inside a quotation. That is why most word rules are warnings.
- No spelling normalisation beyond diacritics (for example different hamza spellings of one word are different words).
- Verb conjugations are matched only in the forms listed in a pack.

## Development
`python -m unittest discover -s tests -v`

TODO before a directory submission: a square PNG icon at `.claude-plugin/icon.png`, and privacy, support and terms links.

## Licence
MIT.
