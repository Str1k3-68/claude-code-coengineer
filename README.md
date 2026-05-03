# Claude Code Coengineer with Codex

A Claude Code plugin that enforces second-source review with OpenAI's `codex exec` for spec sheets, implementation plans, and every code phase. Catches the bugs the primary agent misses, before they hit `git push`.

> _"Pass 14 was Approved. Pass 15 found a real bug. Pass 16 found another. We pay for the 17 reviews so we don't pay for the prod incidents."_

---

## What this enforces

| Trigger | What runs | Hook event |
|---|---|---|
| You edit `docs/superpowers/specs/*.md` | Stop hook reminds you to `codex exec` with `spec-review.md` prompt | PostToolUse Edit/Write/MultiEdit + Stop |
| You edit `docs/superpowers/plans/*.md` | Stop hook reminds you to `codex exec` with `plan-review.md` prompt | same |
| You edit non-trivial code (`src/`, `scripts/`, etc.) | Stop hook reminds you to `codex exec` with `phase-review.md` prompt | same |
| You run `codex exec ...` | Hook records reviewed file paths and clears the relevant reminders | PostToolUse Bash |
| You start a new turn | Hook resets per-turn state | UserPromptSubmit |

The Stop hook **blocks once per stop sequence** if any reminder is outstanding, with a Claude-visible message containing the file list and the suggested prompt template path. Single-fire — won't loop.

The hook is **telemetry, not enforcement** — any internal error degrades to silent exit 0. It will never break Claude Code itself.

---

## Install

### Option 1 — `git clone && ./install.sh` (simplest)

```bash
git clone https://github.com/Str1k3-68/claude-code-coengineer.git
cd claude-code-coengineer
./install.sh
```

The installer:
- Copies `hooks/codex-coengineer/` to `~/.claude/hooks/codex-coengineer/`
- Copies `codex-prompts/*.md` to `~/.claude/codex-prompts/`
- Merges the `hooks` block into your existing `~/.claude/settings.json` (idempotent — safe to re-run)
- Backs up your existing `settings.json` before touching it

### Option 2 — Claude Code plugin marketplace

```
/plugin install Str1k3-68/claude-code-coengineer
```

(Requires that you've added this repo as a marketplace; see `docs/MARKETPLACE.md`.)

---

## Prerequisites

1. **Claude Code** — recent enough to support hook events (`UserPromptSubmit`, `PostToolUse`, `Stop`)
2. **Codex CLI** — `npm install -g @openai/codex` and `codex login`
3. **Python 3.10+** — for the hook itself; no third-party dependencies

Verify with:
```bash
claude --version
codex --version           # codex-cli 0.128.0+
codex login status         # "Logged in using ChatGPT"
```

---

## Quickstart

After install, in any Claude Code session:

1. Edit a spec, plan, or code file
2. Try to end the turn — the Stop hook will block once with a reminder
3. Run codex exec with the suggested prompt template:
   ```bash
   codex exec --skip-git-repo-check -c model_reasoning_effort=xhigh \
       "$(cat ~/.claude/codex-prompts/phase-review.md) -- review src/foo.py" \
       > /tmp/codex.log 2>&1 < /dev/null
   ```
4. The hook detects the codex invocation, registers the reviewed paths, clears the reminder
5. End the turn cleanly

The exact `codex exec` syntax matters. See `docs/CAPTURE_PATTERN.md` for the full rationale on `< /dev/null`, `> file 2>&1`, and `_extract_prompt_text`'s redirect handling.

---

## Configuration

Currently, "non-trivial" path classification is hardcoded for the typical Python+TypeScript project layout (`src/`, `scripts/`, `config/`, etc.). Configurability via `~/.claude/coengineer-config.toml` is planned for v0.2; until then, you can edit the `_NON_TRIVIAL_DIRS` and `_TRIVIAL_DIRS` tuples at the top of `hooks/codex-coengineer/hook.py`.

Spec/plan paths are matched against `docs/superpowers/specs/*.md` and `docs/superpowers/plans/*.md`. If your project uses a different convention, update `_SPEC_PATH_RE` / `_PLAN_PATH_RE`.

---

## What's in the box

```
.
├── hooks/codex-coengineer/
│   ├── hook.py              # 1000-line state machine
│   ├── test_hook.py         # 28 regression tests
│   ├── README.md            # subcommand reference
│   └── settings.example.json
├── codex-prompts/
│   ├── spec-review.md       # for spec sheets
│   ├── plan-review.md       # for implementation plans
│   └── phase-review.md      # for post-phase code review
├── docs/
│   ├── PROCESS.md           # full coengineering workflow
│   ├── CAPTURE_PATTERN.md   # codex exec stdin/buffering rules
│   └── ARCHITECTURE.md      # how the state machine works
├── install.sh               # one-shot installer
└── uninstall.sh
```

---

## Why this exists

Manual discipline alone fails after the 5th hour of a session. A hook makes the discipline structural. This pattern was developed and battle-tested across ~6 months on a quantitative-finance research project where every "trivial" change can move real money. The hook itself went through **17 Codex coengineering passes** during its development — every single Needs-Revision verdict caught a real bug.

See `docs/PROCESS.md` for the full philosophy.

---

## Tests

```bash
python -m pytest hooks/codex-coengineer/test_hook.py -v
```

28 cases covering: URL leak vectors (path-segment, query, fragment, markdown links), absolute-path normalization, `$(cat /path)` substitution (single, multiple, missing, oversize, non-regular, tilde, whitespace, precedence), file redirects (`< /dev/null`, empty file, non-empty file, in-prompt `<`, compact `</path`), redirect operators (`<<EOF`, `>>file`, `2>&1`, `&>>file`), and the spec/plan classifier.

---

## License

AGPL-3.0-or-later. See [LICENSE](./LICENSE).

This is a copyleft license: if you modify this code and use it on a network-accessible service (e.g., a hosted Claude Code instance), you must make the modified source available to users of that service. Forks must also be AGPL-licensed. If those terms don't fit your use case, contact the author for alternative licensing.

---

## Status

**Pre-release / private testing.** APIs and config schema may shift before v0.2. Friend-of-author: please use issues for any rough edges. Public release coming after a few weeks of dogfooding.
