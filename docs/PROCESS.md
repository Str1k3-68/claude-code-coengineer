# Coengineering with Codex inside Claude Code — share-ready process

A practical playbook for using OpenAI's `codex exec` as a second-source reviewer alongside Claude Code (or any primary agent). Distilled from ~6 months of running this on real production work and the post-mortems of multiple failure modes.

---

## 1. Setup

### Plugins (Claude Code)

```
/plugin install superpowers@claude-plugins-official
/plugin install openai-codex@claude-plugins-official     # if you want the slash-command surface; not required
```

**Required:** the `superpowers` plugin. It provides skills like `brainstorming`, `writing-plans`, `executing-plans`, `code-review`, `verification-before-completion`, and `requesting-code-review`. These skills are the connective tissue between "thinking" and "writing code".

**Codex CLI itself** must be installed separately:
```
npm install -g @openai/codex@latest
codex login                # interactive — uses ChatGPT OAuth
```

Verify with:
```
codex --version            # codex-cli 0.128.0 or newer
codex login status         # "Logged in using ChatGPT"
codex exec --skip-git-repo-check "Reply with one word: pong" < /dev/null
```

### Global Claude Code instructions

Drop a file at `~/.claude/CLAUDE.md` with the **codex exec capture pattern** (see §5 below). This file loads in every Claude Code session regardless of project, so the rules don't have to be relearned per repo.

### Project-level configuration

In each project's `CLAUDE.md`, add a "Coengineering Rule" section pointing at the global file. Example:
```markdown
## Coengineering rule

For non-trivial changes (multi-file, production-affecting, security-touching), run `codex exec` for second-source review BEFORE the user commits. See `~/.claude/CLAUDE.md` for the canonical capture pattern. The Stop hook at `~/.claude/hooks/codex-coengineer/hook.py` enforces this and blocks the turn end if it detects edits without a coengineering pass.
```

---

## 2. The three trigger points

Codex is invoked at three explicit points in the workflow. Each has a different prompt structure, and each uses the same capture pattern.

### Trigger 1 — Spec sheet (one-shot, before any planning)

**When:** The user has written or asked the primary agent to write a spec for a non-trivial feature, schema migration, refactor, or production-affecting change. Specs typically live at `docs/superpowers/specs/<date>-<topic>.md`.

**Why review:** Catch ambiguity, missing edge cases, and architectural smells before they get baked into a plan. Cheap to fix at this stage; expensive after.

**What to ask Codex:**
- Is the spec self-consistent? Any contradictions between sections?
- What's the riskiest assumption? What evidence backs it?
- What edge cases are missing (DST, weekends, holidays, partial failures, race conditions, etc.)?
- Is there an implementation that would satisfy the spec but be obviously wrong?
- Verdict: **Approved-To-Plan** or **Needs-Revision** + diffs.

**Iterate** until verdict is `Approved-To-Plan`. Each Codex pass typically catches 1-3 issues; expect 2-4 passes for substantive specs.

### Trigger 2 — Implementation plan (multi-pass, before any code)

**When:** Primary agent (or you) has written a phased implementation plan, typically at `docs/superpowers/plans/<date>-<topic>.md`. The `superpowers` plugin's `writing-plans` skill produces these.

**Why review:** A plan is the contract for the actual work. Phase ordering, API decisions, threshold values, test coverage gaps — all are easier to debate as text than as code. Codex is particularly good at catching "your plan section X says A but section Y implies B" inconsistencies.

**What to ask Codex** (per pass):
- API surface review (signatures, return types, default values)
- Phase ordering (are dependencies correct? can phase N proceed with phase N-1's incomplete state?)
- Test coverage (real-calendar edge cases, fail-toward-red invariants, contract tests for vendored code)
- Cross-project propagation order
- Verdict: **Approved-To-Phase-N** or **Needs-Revision** + specific diffs

**Iterate** the plan based on each verdict. Don't write code until the verdict is `Approved-To-Phase-1` (or whatever the first phase is).

### Trigger 3 — Code review after each implementation phase

**When:** A phase from the plan is complete: code written, tests passing, no regressions in adjacent suites.

**Why review:** Catch silent-green bugs (the gate looks pass but lets stale data through), regressions in tests not yet running, and behavioral drift from what the plan specified.

**What to ask Codex** (per pass, per phase):
- "Read these specific files and confirm the implementation matches the plan's claims"
- "Look for silent-green / silent-success patterns"
- "Threshold math correct?"
- "Edge cases — what's the worst input that could pass this gate?"
- Verdict: **Approved-To-Phase-N+1** or **Approved-To-Commit** or **Needs-Revision**

If `Needs-Revision`, fix and re-run the same Codex prompt. Don't move to the next phase until approved.

---

## 3. The hook (auto-enforcement)

Manual discipline alone fails after the 5th hour of a session. A hook makes the discipline structural.

### What's enforced

The hook lives at `~/.claude/hooks/codex-coengineer/hook.py` and uses the four standard Claude Code hook events as a state machine:

| Hook event | What it does |
|---|---|
| `UserPromptSubmit` | `reset-turn`: clear per-turn state |
| `PostToolUse Edit\|Write\|MultiEdit` | `record-edit`: note that a non-trivial file was touched |
| `PostToolUse Bash` | `record-codex`: detect `codex exec` invocations and note the turn satisfied the rule |
| `Stop` | `check-stop`: if non-trivial files were edited and no codex exec ran, **block the turn end once** with a Claude-visible reminder |

The block message tells the agent (and surfaces to the user): "you modified N files without running codex exec; verify with codex before commit." Single-fire — won't spin in a loop.

### Settings.json wiring (for `~/.claude/settings.json`)

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command",
        "command": "$HOME/.claude/hooks/codex-coengineer/hook.py reset-turn",
        "timeout": 5 }] }
    ],
    "PostToolUse": [
      { "matcher": "Edit|Write|MultiEdit",
        "hooks": [{ "type": "command",
          "command": "$HOME/.claude/hooks/codex-coengineer/hook.py record-edit",
          "timeout": 5 }] },
      { "matcher": "Bash",
        "hooks": [{ "type": "command",
          "command": "$HOME/.claude/hooks/codex-coengineer/hook.py record-codex",
          "timeout": 5 }] }
    ],
    "Stop": [
      { "matcher": "",
        "hooks": [{ "type": "command",
          "command": "$HOME/.claude/hooks/codex-coengineer/hook.py check-stop",
          "timeout": 5 }] }
    ]
  }
}
```

### Reliability principle

The hook is **telemetry, not enforcement**. Any error path (missing file, jq absent, JSON parse error) degrades to silent exit 0. Never break Claude Code itself for a coengineering miss. The block message is Claude-visible feedback; if the agent has good reason to skip codex (truly trivial change, codex unavailable), it can say so explicitly in its next message and the user decides.

### Source for the hook

A working implementation lives in this repo's session at `~/.claude/hooks/codex-coengineer/hook.py`. Copy the directory wholesale for a friend. Key components:
- State per session at `~/.claude/hooks/state/codex-coengineer/<session_id>.json`
- File-locking (`fcntl.flock`) for concurrent hook calls
- Configurable "trivial" filter (don't block on test-only edits, README typos, etc.)
- Optional `CODEX_HOOK_DEBUG=1` env var for verbose tracing

### Optional: spec/plan triggers

The current hook only catches code phases. To extend it to specs and plans, add a fourth recorder:

```json
{ "matcher": "Edit|Write",
  "hooks": [{ "type": "command",
    "command": "$HOME/.claude/hooks/codex-coengineer/hook.py record-spec-or-plan",
    "timeout": 5 }] }
```

The `record-spec-or-plan` subcommand checks the touched file's path against `docs/superpowers/specs/*.md` or `docs/superpowers/plans/*.md` and sets a separate flag. The Stop hook then has two flags to consider: "code-phase needs review" and "spec-or-plan needs review", and surfaces both reminders independently.

I haven't built the spec/plan extension myself yet — it's a 1-hour follow-up. The state-machine architecture supports it cleanly.

---

## 4. Calling Codex — the capture pattern

This is **the** lesson from the post-mortem that motivated this whole document.

### Do this

```bash
codex exec --skip-git-repo-check -c model_reasoning_effort=xhigh "..." > /tmp/codex.log 2>&1
# After exit, any duration: cat /tmp/codex.log
```

For long runs where you want progress visibility:
```bash
( codex exec --skip-git-repo-check -c model_reasoning_effort=xhigh "..." > /tmp/codex.log 2>&1 ) &
# In a separate Bash call: tail -f /tmp/codex.log
```

### Don't do this

```bash
codex exec ... 2>&1 | tail -80    # ❌ tail -N discards EVERYTHING if codex is killed pre-EOF
codex exec ...                    # ❌ stdout-only capture misses stderr (where progress lives)
```

### Liveness check

Codex writes a session rollout file in real time, independent of stdout/stderr capture. Use it as ground truth for "is codex alive":
```bash
ls -lt ~/.codex/sessions/$(date +%Y/%m/%d)/*.jsonl | head -1
```
If the file's mtime is recent and growing, codex is working. If it hasn't changed in 60+ seconds, it's actually stuck.

### Patience budget

Empirical timings on a recent MacBook Pro M-series, codex-cli 0.128.0, gpt-5.5, xhigh effort:
- Trivial prompt, no tool use: 5-10 s
- Single small file read: 10-30 s
- 4-5 file reads + reasoning: 30-90 s
- Heavy review (15+ tool calls, 4500-char prompt, multi-round reasoning): up to 2 minutes

Don't `pkill` codex before 5 minutes. Don't pkill based on "0 bytes in capture file" — that's the `tail -N` trap.

### Reasoning effort

Default to `model_reasoning_effort=xhigh` for substantive review. The user paid for the tokens; use them. Use `medium` only when a specific time budget demands it.

### The two-fd-into-one-file pattern

```bash
codex exec ... > /tmp/codex.log 2>&1
```
Both stdout (final agent message) and stderr (banner/reasoning/tool calls) end up in the same file. Cat it after exit; you get the full transcript.

If you need to keep them separate (e.g., final answer goes to a different consumer than the progress feed):
```bash
codex exec ... > /tmp/codex.out 2> /tmp/codex.err
```

### Streaming mode

For machine-readable streaming output:
```bash
codex exec --json ... < /dev/null
```
Each line is a complete JSONL event; flushed in real time. Parse as it arrives.

---

## 5. Common failure modes

### Failure: "Codex is hung — 0 bytes after 10 minutes"

99% of the time: you used `tail -N` to capture, the agent harness has been running codex for 90 seconds, and you're about to `pkill` work that would have completed in another 30 seconds.

**Diagnostic:** check the rollout file mtime. If recent, wait. If stale, *then* pkill.

### Failure: "Codex review keeps approving things that turn out to be wrong"

Codex is a second source, not an oracle. Its track record on this project (~30 reviews) is:
- **Good at:** API surface review, threshold math, plan phase ordering, missing edge cases, "you said X here and Y there" inconsistencies
- **Mediocre at:** holistic architectural feedback (it tends to focus on what you ask)
- **Poor at:** Things outside its read window (it samples files; doesn't always read the whole repo)

**Mitigation:** ask narrow, specific questions. "Is this _check_freshness function silent-green-safe?" beats "review this PR".

### Failure: "We approved the plan but the implementation diverged"

Plans drift in implementation. Codex post-implementation review (Trigger 3) is the catch. Don't skip it because Trigger 1+2 went well.

### Failure: "Codex told me X but X was wrong"

Codex makes mistakes. Today (2026-05-03) Codex correctly identified that my GitHub issue draft was technically wrong — but had I not also empirically tested its claim, I'd have either filed a bad issue or backed off without confirming. **Treat Codex as a peer, not an authority.** Verify its claims when stakes are non-trivial.

### Failure: "The hook is annoying / blocks too often"

Tune the trivial filter. Test-only edits, README typos, comment-only edits, removing dead imports — these don't need coengineering. Add path patterns to the hook's trivial-edit list.

### Failure: "I can't tell if a coengineering pass actually happened"

Each codex exec creates a rollout file at `~/.codex/sessions/YYYY/MM/DD/*.jsonl` with full conversation history. Audit trail is permanent.

---

## 6. The complete recipe per turn

For a non-trivial code change:

1. **Spec written** → `codex exec --skip-git-repo-check -c model_reasoning_effort=xhigh "$(cat spec_review_prompt.md)" > /tmp/codex.log 2>&1` → iterate until `Approved-To-Plan`.
2. **Plan written** → same pattern, prompt asks plan-review questions → iterate until `Approved-To-Phase-1`.
3. **Phase 1 code written + tests passing** → same pattern, prompt asks phase-1-review questions → iterate until `Approved-To-Phase-2` or `Approved-To-Commit`.
4. **Phase N+1...** → repeat 3.
5. **Commit** with body referencing the codex review verdicts and rollout-file paths.

Each step's prompt is just a markdown file you `cat` into the codex command. Build a library of these prompts (e.g., `~/.claude/codex-prompts/spec-review.md`, `~/.claude/codex-prompts/plan-review.md`, `~/.claude/codex-prompts/phase-review.md`) so you're not rewriting them per session.

---

## 7. Tradeoffs

- **Cost:** Codex xhigh on a substantive review burns ~50K-200K tokens per pass. At current pricing this is non-trivial. The bug-catch rate makes it worth it for production-affecting work; skip it for prototypes and experiments.
- **Latency:** Each pass takes 30-120 seconds. A multi-pass plan review can add 10-15 minutes to a session. Real, but small compared to a missed bug.
- **Cognitive load:** You're running two agents in parallel. Don't do this for trivial work — the overhead exceeds the value.
- **Trust calibration:** Codex disagreements with the primary agent are signal. Most disagreements I've seen surface real bugs in the primary agent's reasoning. A few have been Codex hallucinations. Spend the cycles to figure out which.

---

## 8. What this doesn't replace

- Human review at PR time
- Tests
- The `superpowers` plugin's other skills (brainstorming, writing-plans, executing-plans, etc.)
- Reading the code yourself before merging

It's a **second-source filter** that catches a class of bugs the primary agent misses. Useful, not magical.

---

*Distilled from ~6 hours of debugging the wrong things on 2026-05-03 and 6 months of running this process on a real Composer.trade research and trading project.*
