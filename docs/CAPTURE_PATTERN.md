# `codex exec` capture pattern

This is THE rule for invoking `codex exec` from Bash, agent harnesses, CI, or any non-TTY parent process. Distilled from a 6-hour debugging session in May 2026 where every substantive `codex exec` looked hung but was actually working fine.

## The recipe

```bash
codex exec --skip-git-repo-check -c model_reasoning_effort=xhigh \
    "your prompt here" \
    > /tmp/codex.log 2>&1 \
    < /dev/null
```

Three pieces matter:

| Piece | Why |
|---|---|
| `> /tmp/codex.log 2>&1` | Capture both stdout (final agent message) AND stderr (banner / reasoning / tool calls) into one file. **Read it after the process exits**, with `cat /tmp/codex.log`. |
| `< /dev/null` | Cheap insurance against `codex exec` blocking on a stdin read in non-TTY contexts. The CLI prints `Reading additional input from stdin...` and tries to concatenate stdin with the positional prompt. With pipe-stdin, that read can hang if the parent never writes EOF. |
| `-c model_reasoning_effort=xhigh` | Maximum reasoning effort. You're paying tokens for a second-source review; use them. Drop to `medium` only when a specific time budget demands it. |

## Common mistakes

### `tail -N` capture

```bash
codex exec ... 2>&1 | tail -80    # ❌ DON'T
```

`tail -N` is a buffered sink — it only emits anything after EOF on its input. While codex is running, all output sits in tail's buffer. If you `pkill` codex (timeout, frustration, etc.), tail sees a broken pipe and discards everything. The captured output ends up empty even though codex was actively working.

This single mistake was the entire root cause of the original 6-hour debugging session.

### Watching only stdout

In **human mode**, `codex exec` puts the streaming progress (banner, reasoning, `exec` lines, tool outputs) on **stderr**. Only the final agent message goes to **stdout**. Watching stdout alone shows nothing for the entire run.

Always merge with `2>&1` (or capture them separately if you want to distinguish progress from final).

### Impatience

Empirical timings on a recent MacBook Pro M-series, codex-cli 0.128.0, gpt-5.5, xhigh effort:

| Workload | Duration |
|---|---|
| Trivial prompt, no tool use | 5–10 s |
| Single small file read | 10–30 s |
| 4–5 file reads + reasoning | 30–90 s |
| Heavy review (15+ tool calls, 4500-char prompt, multi-round reasoning) | up to 2 min |

**Don't `pkill` codex before 5 minutes elapsed** for substantive reviews. Don't pkill based on "0 bytes in capture file" — that's almost always the `tail -N` trap above.

## Liveness check (is it really hung?)

Codex writes a session rollout file in real time, independent of stdout/stderr capture:

```bash
ls -lt ~/.codex/sessions/$(date +%Y/%m/%d)/*.jsonl | head -1
```

If the file's mtime is recent and the size is growing, codex is alive. If size hasn't changed in 60+ seconds, **then** it's actually stuck.

The rollout file records every tool call, every reasoning block, and the final `task_complete` event. Use it as the ground truth.

## Streaming mode for automation

For machine-readable streaming output (event per line, flushed in real time):

```bash
codex exec --json --skip-git-repo-check -c model_reasoning_effort=xhigh \
    "your prompt" < /dev/null
```

Each line is a complete JSONL event. A parser can react as Codex emits — useful for CI, dashboards, or another agent listening for verdicts.

## Why the hook handles this

The `codex-coengineer` hook's `_extract_prompt_text` understands all four invocation forms:

1. Heredoc body (`<<EOF ... EOF`)
2. File redirect (`< /tmp/prompt.txt`) — reads the file
3. Command substitution (`$(cat /tmp/prompt.md)`) — reads each cat'd file with safety bounds
4. Inline quoted positional (`"prompt text"`)

Plus token-aware redirect handling so an in-prompt `<` (e.g., `"compare a < b in src/foo.py"`) isn't misread as a shell redirect, and compact forms like `</tmp/path` are split correctly. See `hooks/codex-coengineer/test_hook.py` for the 28-case attack surface.

## Why this section is its own document

Every coengineering pattern depends on this. If you don't capture codex output correctly, you can't tell whether a review actually happened. So keep this doc near the top of your reading order before you build process around codex.
