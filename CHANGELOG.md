# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## [0.1.2] (2026-11-16)
Fixes from the second adversarial round.
- Worker process: it now has its own hard deadline (timeout plus 3 s, armed with `faulthandler`, which fires even inside a long regex match), so it ends by itself if the parent is killed. `--_worker` is refused unless the parent started it (a one-time token passed in an environment variable and in the work order on stdin). The work order travels on stdin, so a long command line can no longer break the child, and there is no longer any fall-back that runs without a time limit: if the worker cannot start, the run fails with a message.
- A timeout no longer discards earlier results: the findings of every file already linted are printed, and the error names the file and the rule that was running (JSON output gets `"complete": false`). Exit code stays 1 (tool error).
- Tagged scope is no longer silent: the summary prints `N untagged line(s) skipped (use --all-lines)` and the JSON has `untagged_skipped` and `scope_note`. A tag after a leading RLM, LRM, ZWSP, BOM, NBSP or bidi control is recognised.
- Regex guard: also limits optional parts (6), wide repeats (4 in total, 2 in a row; a bounded repeat above 10 counts as wide). Still a heuristic: some slow patterns pass (for example separate groups that each contain a repeat); the timeout is the real guard.
- Wordlist rules are matched by tokens and a lookup table instead of one huge regex: 40 rules of 500 words over 20 long lines runs in well under a second (it took about 18 s). Matching also normalises decomposed hamza/madda and supports two stacked prefixes (و + ال).
- Low items: rule and pack ids may not end with a newline or be device names (con, nul, aux, com1...); `tag_pattern` and every echoed pack string are sanitised in JSON; long-line warnings obey the 500 cap and `--line-filter`; pack file names are matched case-exactly in both loading and `--list-packs`; `--list-packs` reports how many files were not listed; device names and non-regular files are refused as inputs.
- Saudi pack: added والسنوات and other stacked prefixes, بالذي/للذي/الذى/التى, لأنه/لأنها/لأنهم/لكنه/لكنها, الان and اين without hamza, and the sign ٪. Removed false positives: شكراً/طبعاً (both tanween orders) and دايمًا no longer flagged, «30» is ignored like Latin inside «…», and "أشهر" is flagged only after a number word (ثلاثة أشهر), not in "most famous".
- Skills: `lahja-pack` must not overwrite an existing file, keeps paths inside the project folder, refuses reserved ids, and may only edit the pack file it created.
- Known limits: the heuristic guard; Python 3.8 and Linux not run; hamza-less لان, kasra on kaf, full-width digits, ZWNJ inside words are still not handled; a word quoted in «…» is still flagged by formal-particles; the `lahja.py *` permission still allows `--timeout 600`.

## [0.1.1] (2026-11-16)
- Per-pack scope: a pack may declare `"scope": {"mode": "tagged", "tag_pattern": "..."}` to lint only lines that match the tag pattern (for example `[narrator] ...`); the matched tag is hidden from the rules and columns still refer to the original line. `"scope": {"mode": "all"}` is the default, so existing packs behave as before. The scope is validated strictly (unknown mode or key, missing, invalid or too long regex, the same regex guard as for rules).
- The built-in `saudi` pack now uses tagged scope with the tag pattern `^\s*\[[^\]]+\]`. Use `--all-lines` to lint every line as before.
- New flags `--all-lines` and `--tagged-only` (mutually exclusive) override the pack's scope for one run. `--line-filter` still works and combines with the scope: a line must pass both.
- `--list-packs` shows each pack's scope, and `--format json` output has a `scope` field.
- Icon added (square 1024 px PNG, generated with Gemini and chosen by the author).

## [0.1.0] (2026-11-15)
- First version: one standard-library Python script, `scripts/lahja.py`, that lints spoken-word scripts against a dialect pack and reports `file:line:col`. Read-only, no network.
- Rule kinds: `regex`, `wordlist` (whole words, diacritics ignored, optional prefixes and per-word fix), `numeral`, `latin`, `length`; optional `allow` and `ignore_inside` on every rule; optional line filter in the pack or on the command line.
- Packs are JSON files, validated with clear errors: built-in `packs/saudi.json`, user packs through `--pack-dir`, an example pack in `examples/packs/`, and `--list-packs` to list and validate.
- Safety: structural regex guard (nested repeats, optional parts or alternation in a repeated group, backreferences, inline verbose flag, size caps), a watchdog child process with `--timeout`, caps on file, pack and line size, control and bidi characters removed from output.
- Exit codes: 0 clean, 2 error findings, 1 tool error. Text and JSON output.
- Two skills: `lahja` (lint, show findings, ask before any edit) and `lahja-pack` (create a dialect pack with the user's approval). Both grant only this plugin's script.
