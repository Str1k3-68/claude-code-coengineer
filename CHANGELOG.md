# Changelog

## v0.1.0 — 2026-05-03 (private testing)

Initial scaffold. Includes:

- `hooks/codex-coengineer/hook.py` — state-machine Stop hook
- `hooks/codex-coengineer/test_hook.py` — 28-case regression suite
- `codex-prompts/{spec,plan,phase}-review.md` — starter prompt library
- `install.sh` / `uninstall.sh` — idempotent installer with `~/.claude/settings.json` merge
- `.claude-plugin/plugin.json` — Claude Code plugin manifest
- `docs/{PROCESS,CAPTURE_PATTERN,ARCHITECTURE}.md` — full reference

### Bugs caught and fixed during 17 Codex coengineering passes (development)

| Pass | Bug |
|---|---|
| 2 | Absolute-path extraction missing |
| 3 | URL path-segment leak (`https://github.com/.../docs/X.md` → false match) |
| 4 | URL query/fragment leak (`?file=X.md` and `#X.md`) |
| 5 | Repo copy out-of-sync with global; missing tests |
| 9 | `$(cat ...)` failed-read silent pass (literal `$(cat /missing.py)` extracted as path) |
| 10 | `$(cat` literal-substring guard missed whitespace variant `$( cat ...)` |
| 11 | Second literal-substring guard inside `_expand_cat_substitutions` |
| 13 | `< /dev/null` short-circuit (read empty content, returned without falling through) |
| 14 | Step-2 redirect detection used regex on raw command, matched in-quote `<` |
| 15 | Compact `</tmp/path` form missed by exact-match token check |
| 16 | Compact-redirect regex alternation ordered shortest-first (`<<EOF` split as `<` + `<EOF`) |

Every fix has a regression test in `test_hook.py`.
