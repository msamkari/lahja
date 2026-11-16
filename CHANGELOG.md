# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## [0.1.0] (2026-11-15)
- First version: one standard-library Python script, `scripts/lahja.py`, that lints spoken-word scripts against a dialect pack and reports `file:line:col`. Read-only, no network.
- Rule kinds: `regex`, `wordlist` (whole words, diacritics ignored, optional prefixes and per-word fix), `numeral`, `latin`, `length`; optional `allow` and `ignore_inside` on every rule; optional line filter in the pack or on the command line.
- Packs are JSON files, validated with clear errors: built-in `packs/saudi.json`, user packs through `--pack-dir`, an example pack in `examples/packs/`, and `--list-packs` to list and validate.
- Safety: structural regex guard (nested repeats, optional parts or alternation in a repeated group, backreferences, inline verbose flag, size caps), a watchdog child process with `--timeout`, caps on file, pack and line size, control and bidi characters removed from output.
- Exit codes: 0 clean, 2 error findings, 1 tool error. Text and JSON output.
- Two skills: `lahja` (lint, show findings, ask before any edit) and `lahja-pack` (create a dialect pack with the user's approval). Both grant only this plugin's script.
