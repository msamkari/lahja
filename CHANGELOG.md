# Changelog

This project follows [Semantic Versioning](https://semver.org/).

- Icon added (square 1024 px PNG, generated with Gemini and chosen by the author).

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
