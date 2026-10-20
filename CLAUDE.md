# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
python3 -m unittest discover -s tests                                   # all tests
python3 -m unittest tests.test_lager_suche.SearchTests.test_typo_is_found_fuzzy   # single test
python3 lager_suche.py --data /path/inventar.csv wo ist galaxy quest   # manual query
python3 lager_suche.py --data /path/inventar.csv --import export.zip   # manual import
```

No dependencies, no build step, no linter configured.

## Constraints

- **Standard library only.** `lager_suche.py` runs via `shell_command` inside the Home Assistant Core container, where pip packages must not be installed.
  Exception: the optional [reveries](https://github.com/gman-k8s/reveries) quirks are imported lazily from the cloned `../reveries` folder (needs PyYAML, present in HA Core). Search must keep working when it is missing or broken.
- **Never commit real inventory data.** `*.csv`/`*.zip` are gitignored except `tests/fixtures/`. `config.json` and `.env` are local-only. No bot tokens or Telegram user IDs in the repo, including `nodered/csv_upload_flow.json` (secrets live in Node-RED globals set by the user's "Set Tokens" node).
- User-facing output is German and Telegram-sized (≤ `max_chars`, default 4000).

## Architecture

Everything is in `lager_suche.py`. The pipeline:

1. **Read**: `parse_inventory` decodes the bytes (utf-8-sig → cp1252), sniffs the delimiter from the header, then maps columns via `config["columns"]` (lists of candidate header names; `name` and `location` are required).
2. **Request parsing**: `parse_request` turns the raw Telegram text into `(mode, arg)`: `search`, `box` ("kiste N"/"kiste küchenkram") or `overview` ("lager"). It strips prefixes like "wo ist" and leading articles. The HA automation passes the whole message text, so prefix handling lives here, not in HA.
3. **Search** is tiered: exact/word-start matches (`exact_score`, with simple German plural stemming), then word-inside matches only when nothing better exists. Fuzzy `difflib` matching runs only when there are no substring hits. "Meintest du" suggestions come only with zero hits.
4. **Location model**: a Nest Egg `Ort` is a path like `Keller » Kiste 4`. `Item.place` is the first segment and `Item.container` the rest. Output renders this as `Kiste 4 · Keller`.
5. **Import**: `import_file` accepts the Nest Egg share ZIP (`Artikel.csv` + `Orte.csv` + `Kategorien.csv`) or a bare CSV. It validates by fully parsing, copies the old file to `inventar_alt.csv`, then swaps in the new file atomically via a temp file and `os.replace`. An invalid upload leaves the old inventory untouched.
6. **Quirks** (optional): `main()` calls `answer(..., quirks=True)`, which passes unambiguous, non-fuzzy single-box search results through `with_reveries` (facts `kiste`, `ort`, `titel`). `answer()` defaults to `quirks=False` so tests stay deterministic; `CliTests` sets `REVERIES_DIR=""` for the same reason.

Integration around it (outside this repo, on HA): Node-RED Telegram bridge → `telegram_text` event → "Telegram: Smart Router" automation → `shell_command.lager_suche`. Document uploads go Node-RED → `/homeassistant/nestegg/upload/upload.bin` → `nestegg_csv_uploaded` event → `shell_command.lager_import`. `script.lager_suche` exposes the search to the Assist/LLM agent. See README.md for paths and snippets.
