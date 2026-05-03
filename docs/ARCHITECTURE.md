# Architecture

## State machine, not transcript scraping

The `codex-coengineer` hook is a **state machine over Claude Code hook events**. Four hook events map to four subcommands:

```
UserPromptSubmit  →  reset-turn        (clear per-turn state)
PostToolUse Edit  →  record-edit       (note touched file + classify)
PostToolUse Bash  →  record-codex      (detect codex exec invocations)
Stop              →  check-stop        (read state, decide to block once)
```

Per-session state lives at `~/.claude/hooks/state/codex-coengineer/<session_id>.json`, locked via `fcntl.flock` for read-modify-write across parallel hook invocations.

Why state-machine and not transcript scraping? Codex's original design feedback (2026-04-08): scraping is fragile (tool output formats change), expensive (re-parses everything every Stop), and ambiguous (when did "this turn" actually start?). The state machine is deterministic, cheap, and correct.

## Two independent reminder streams

A single Stop hook check produces a combined block message with up to two sections:

### Section 1 — code phase

Triggered by edits to non-trivial files (code, configs, schemas) **without** a matching `codex exec` invocation in the same turn.

"Matching" means: codex exec's prompt mentioned the edited file path (suffix-aware to handle absolute vs repo-relative). Coverage is union'd across all codex_runs in the turn — so the workflow `edit → codex review → fix the findings → commit` doesn't fire false positives for the post-review fixes.

### Section 2 — spec/plan freshness

Triggered by edits to `docs/superpowers/specs/*.md` or `docs/superpowers/plans/*.md` **without** a matching `codex exec` since the last touch.

Per-path tracking: `state["spec_plan_docs"][path] = {kind: "spec"|"plan", touched_ts, reviewed_ts}`. The Stop check fires for any doc where `touched_ts > reviewed_ts` (or `reviewed_ts is None`). This is independent of the code-phase check — `docs/` is normally classified trivial for code coverage, but spec/plan docs always get their own review pass.

Both sections fire together when both apply, in one combined block.

## Single-fire semantics

Claude Code's Stop hook protocol provides `stop_hook_active: bool` in the input. If True, this hook already blocked once this stop sequence — exit 0 to allow the stop and prevent infinite loops. The hook respects this strictly.

The block message itself signals the same: "Single-fire reminder — won't block again on this stop sequence." Claude can either run codex and re-stop cleanly, or explicitly say "skipping codex because trivial" and let the user decide.

## Path extraction from codex prompts

The riskiest function is `_extract_reviewed_files` — given a bash command string, what file paths did the user ask codex to review? Wrong answer falsely covers an edit (silent green) or fails to cover one (false fire).

The implementation went through 17 Codex coengineering passes during development. Final architecture:

```
1. _extract_prompt_text(command) → str
   ├── Heredoc body (regex, multiline)
   ├── shlex.split + compact-redirect splitter (token-aware)
   ├── First stdin redirect target (read file if non-empty + not /dev/null)
   ├── $(cat <path>) substitution (with size + regular-file safety bounds)
   └── Longest non-flag, non-redirect, non-keyword positional

2. _URL_RE.sub(" ", prompt) → strip URLs (defeats query/fragment leaks)

3. _PROJECT_PATH_RE — extract repo-relative paths
   _ABSOLUTE_PROJECT_PATH_RE — extract absolute paths whose tail
                               starts with a known project-dir, then
                               normalize to repo-relative

4. Cap at _MAX_REVIEWED_FILES_PER_RUN (50) to bound state size
```

## Spec/plan classification

```
_SPEC_PATH_RE = (?:^|/)docs/superpowers/specs/[\w./\-]+\.md$
_PLAN_PATH_RE = (?:^|/)docs/superpowers/plans/[\w./\-]+\.md$
```

These are the conventions used by the `superpowers` plugin's `writing-plans` skill. If your project uses different paths, edit the regexes at the top of `hook.py`. Future v0.2 work: make these configurable via `~/.claude/coengineer-config.toml`.

## Reliability — telemetry, not enforcement

Every error path degrades to silent exit 0:
- Missing state file → fresh state
- Malformed JSON → fresh state
- Lock acquisition fails → no-op
- Path extraction error → empty list (defaults to "no coverage")
- Block message JSON serialization fails → no block (allow stop)

The hook **will never break Claude Code itself**. Worst case: a missed reminder. Best case: a caught silent-pass before commit.

## What's NOT in scope

- **Running codex automatically** — the hook reminds you, doesn't drive. The user is in control of when to run review.
- **Caching codex verdicts** — each run is independent; the hook only tracks "was codex called with this file mentioned" not "what did codex say."
- **Cross-session continuity** — state is per-session-id. A new session starts fresh.
- **Cloud sync** — state lives on the local filesystem. No telemetry, no remote logging.

## Test surface

`hooks/codex-coengineer/test_hook.py` has 28 cases. The path-extractor cases are organized by attack vector:

| Vector | Cases |
|---|---|
| URL leaks | path-segment, query-string `?file=`, fragment `#`, markdown link, http/https |
| Absolute path | `/Users/.../docs/.../X.md`, `/tmp/src/foo.py`, `/Users/user-with-src-in-name/x.py` (false-positive guard) |
| `$(cat)` substitution | single, multiple, missing file fallback, oversize, non-regular `/dev/zero`, tilde, whitespace, precedence below heredoc, precedence below redirect, readable whitespace variant, silent-pass with project-dir tail |
| Stdin redirect | `< /dev/null` falls through, empty file falls through, non-empty wins over inline, in-prompt `<` not misread |
| Compact redirect | `</tmp/path`, `</dev/null`, `>/tmp/log 2>&1`, `>>file`, `2>>file`, `&>>file` |
| Spec/plan classifier | spec, plan, absolute spec, non-spec docs, non-md extension, src code |

Every Needs-Revision verdict during development corresponds to a test case here. Run before committing any change to `hook.py`:

```bash
python -m pytest hooks/codex-coengineer/test_hook.py -v
```
