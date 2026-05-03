# codex-coengineer hook

Subcommand reference. Wired to four Claude Code hook events.

| Subcommand | Hook event | What it does |
|---|---|---|
| `reset-turn` | UserPromptSubmit | Clears per-turn state (edits, codex_runs, spec_plan_docs touch flags). |
| `record-edit` | PostToolUse Edit/Write/MultiEdit | Appends the touched file to state. Classifies as non-trivial (code) or spec/plan (separate flag). |
| `record-codex` | PostToolUse Bash | Detects `codex exec` invocations. Extracts file paths from the prompt body (heredoc, file redirect, `$(cat)`, inline arg). Updates spec/plan `reviewed_ts` for matched paths. |
| `check-stop` | Stop | Reads state. If non-trivial code edits OR spec/plan touches without matching codex review, blocks once with a Claude-visible reminder containing file list + suggested prompt template path. |

## State

Per-session state at `~/.claude/hooks/state/codex-coengineer/<session_id>.json`:

```json
{
  "session_id": "...",
  "turn_started_at": 1234567890.0,
  "edits": [
    {"file": "...", "tool": "Edit", "ts": ..., "non_trivial": true}
  ],
  "codex_runs": [
    {"ts": ..., "cmd_preview": "...", "reviewed_files": ["src/foo.py"]}
  ],
  "spec_plan_docs": {
    "docs/superpowers/specs/X.md": {
      "kind": "spec",
      "touched_ts": ...,
      "reviewed_ts": ... | null
    }
  }
}
```

State is locked via `fcntl.flock` for read-modify-write — parallel hook calls (record-edit and record-codex from concurrent PostToolUse events) are serialized.

## Reliability

Telemetry, not enforcement. Any error path (missing state file, malformed JSON, missing dependencies) degrades to silent exit 0. The hook will never break Claude Code itself.

To inspect what the hook is doing:

```bash
export CODEX_HOOK_DEBUG=1
# Run a Claude Code session; per-event lines land in:
tail -f ~/.claude/hooks/state/codex-coengineer/debug.log
```

## What's "non-trivial"

The classifier walks the file path left-to-right, finding the first match against:

| Tier | Behavior |
|---|---|
| Always-trivial substring (`__pycache__/`, `results/logs/`, `.venv/`, `node_modules/`, etc.) | trivial |
| Always-trivial extension (`.md`, `.lock`, `.html`, `.log`, `.bak`, `.tmp`) | trivial |
| Specific lockfiles + `MEMORY.md` | trivial |
| First path component matches `_NON_TRIVIAL_DIRS` (`src/`, `scripts/`, `config/`, `schema/`, `migrations/`, `reference/`, `symphonies/`, `tools/`) | non-trivial |
| First path component matches `_TRIVIAL_DIRS` (`tests/`, `docs/`, `reports/`, `memory/`, `_deprecated/`) | trivial |
| Falls back to extension list (`.py`, `.sh`, `.sql`, `.yaml`, `.yml`, `.toml`, `.json`) | non-trivial |
| Otherwise | trivial |

Spec and plan sheets are tracked **separately** from non-trivial code, regardless of `_TRIVIAL_DIRS` membership — they live under `docs/` (which is otherwise trivial) but get their own coengineering pass.

## What counts as a "real" codex invocation

The hook ignores `echo`'d strings, comments, and shell-quoted false positives. The detector is a regex (`codex\s+\S+`) anchored to a real word boundary, with a guard that rejects commands starting with `echo `, `printf `, `# `, etc.

## Path extraction from codex prompts

Step-by-step (token-aware as of v0.1.0):

1. **Heredoc** — `<<EOF ... EOF`. Multiline regex; can't false-positive on quoted content.
2. **`shlex.split`** — tokenize once. Preserves quoted prompts as single tokens.
3. **Compact-redirect splitter** — splits `</tmp/path` and `2>>file` (single shlex tokens) into operator + target tokens.
4. **Token walker** — identifies the first stdin-redirect target (if any), drops other redirect operators + targets.
5. **Stdin redirect** — if the redirect target is non-empty and not `/dev/null`, return its contents.
6. **`$(cat <path>)` substitution** — if present, expand each cat'd file (with size + regular-file safety bounds) and return concatenated content. Commits to cat semantics; never falls through.
7. **Inline positional** — pick the longest non-flag, non-keyword positional argument.

This avoids the URL-leak, in-prompt-`<`, and silent-pass bugs that simpler implementations exhibit (see test cases in `test_hook.py` for the full attack surface).
