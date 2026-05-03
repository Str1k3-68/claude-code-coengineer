#!/usr/bin/env python3
"""Codex coengineering enforcement hook for Claude Code.

Architecture (per Codex design review 2026-04-08): state-machine, not
transcript scraping. Four subcommands map to four hook events:

  reset-turn      → UserPromptSubmit       (clear per-turn state)
  record-edit     → PostToolUse Edit|Write|MultiEdit  (note touched file)
  record-codex    → PostToolUse Bash       (note codex exec invocation)
  check-stop      → Stop                   (read state, decide to block once)

State per session_id at:
  ~/.claude/hooks/state/codex-coengineer/<session_id>.json

The Stop hook BLOCKS ONCE if non-trivial files were touched and no
codex exec was run since the turn started. The block message becomes
Claude-visible feedback. The `stop_hook_active` field in the Stop
event input tells us if we already blocked this stop sequence — in
that case we exit 0 to prevent infinite loops (the user docs require this).

Reliability principle: this is telemetry, not enforcement. Any error
path (missing file, malformed JSON, missing jq, exception) degrades
to silent exit 0. We never want to break Claude Code itself.

Reference:
  - Memory: feedback_codex_coengineering.md
  - Codex review: 2026-04-08
  - Hooks docs: https://code.claude.com/docs/en/hooks-guide
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".claude" / "hooks" / "state" / "codex-coengineer"
DEBUG_LOG = STATE_DIR / "debug.log"

# Set CODEX_HOOK_DEBUG=1 to write per-event lines to debug.log so you
# can confirm the hook is firing. Off by default to keep things quiet.
DEBUG_ENABLED = os.environ.get("CODEX_HOOK_DEBUG") == "1"


def _debug_log(msg: str) -> None:
    """Append a debug line if CODEX_HOOK_DEBUG=1."""
    if not DEBUG_ENABLED:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(DEBUG_LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except Exception:
        pass


_SAFE_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _sanitize_session_id(session_id: str) -> str:
    """Reduce session_id to a filename-safe slug.

    Codex Pass-1 hardening note: session_id flows directly into state +
    lock filenames. While Claude Code controls this value, defensive
    coding prevents any path-traversal or wildcard surprises if the
    schema evolves.
    """
    if not session_id:
        return ""
    # Cap length, replace any unsafe chars with `_`.
    s = _SAFE_SESSION_ID_RE.sub("_", session_id)[:128]
    # Reject `..`-style names entirely.
    if not s or s in (".", "..") or s.startswith("."):
        return ""
    return s


@contextlib.contextmanager
def _locked_state(session_id: str):
    """Acquire an exclusive flock on the state file for read-modify-write.

    Codex Stage 3a-hook finding: parallel PostToolUse hooks (record-edit
    and record-codex) can race on read-modify-write of the same state file,
    silently dropping events. fcntl.flock is the standard Unix solution.

    Yields (state_dict, save_callback). The lock is held for the entire
    yielded block. On any exception, the lock is released and we degrade
    to silent exit 0 like the rest of the script.
    """
    sid = _sanitize_session_id(session_id)
    if not sid:
        yield _fresh_state(""), lambda s: None
        return

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / f"{sid}.lock"
    state_path = STATE_DIR / f"{sid}.json"

    lock_fd = None
    try:
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)

        # Load latest state INSIDE the lock
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
            except Exception:
                state = _fresh_state(session_id)
        else:
            state = _fresh_state(session_id)

        def _save(new_state: dict) -> None:
            try:
                # Atomic + per-session tmp (avoids cross-session tmp collision)
                tmp = state_path.with_suffix(f".tmp.{os.getpid()}")
                tmp.write_text(json.dumps(new_state, indent=2))
                os.replace(tmp, state_path)
            except Exception:
                pass

        yield state, _save
    except Exception:
        # Lock acquisition failed or other error — degrade silently
        yield _fresh_state(session_id), lambda s: None
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                lock_fd.close()
            except Exception:
                pass

# ── Trivial / Non-trivial classification (v3 unified) ───────────────
#
# Single source of truth for directory classification. Both the edit
# classifier (`_is_non_trivial`) and the path extractor (`_PROJECT_DIRS`)
# derive from these tuples to prevent drift.
#
# Codex round-3 finding (2026-04-08): the old classifier had three drifting
# lists (NON_TRIVIAL_PATTERNS, TRIVIAL_PATTERNS, _PROJECT_DIRS) that didn't
# include the same directories. Most visibly, `tools/` was missing entirely,
# so editing the hook itself or its README couldn't be tracked. Unified.
#
# Classification semantics (root-dir, walk-left-to-right):
#   1. Always-trivial substrings ALWAYS win (__pycache__, results/logs,
#      results/cache) regardless of parent dir
#   2. Always-trivial extensions (.md, .lock, .html, .log, .bak, .tmp)
#      and specific lockfile names always win
#   3. Walk path components left-to-right; first component matching either
#      _NON_TRIVIAL_DIRS or _TRIVIAL_DIRS wins. Per Codex's recommendation,
#      this is a "first project-root directory" semantic, not "any segment
#      anywhere matches." Example: tools/tests/foo.py is non-trivial because
#      `tools` comes first in the walk, even though `tests` appears later.
#   4. If no project dir matched, fall back to non-trivial extension list

_NON_TRIVIAL_DIRS = (
    "src",
    "scripts",
    "config",
    "schema",
    "migrations",
    "reference",
    "symphonies",
    "tools",         # added in v3 for the hook itself + collaborator docs
)

_TRIVIAL_DIRS = (
    "tests",
    "test",          # singular form
    "docs",
    "doc",           # singular form
    "reports",
    "report",
    "memory",        # auto-memory dir (still trivial — no Codex coverage needed)
    "_deprecated",   # files moved here are no longer production
)

# NOTE: `results` is intentionally NOT in either list. Bare `results/foo.json`
# (e.g., calibration outputs) is meaningful and should be non-trivial via the
# extension fallback. Only specific subdirs of `results/` are trivial (logs,
# cache) — handled by _ALWAYS_TRIVIAL_SUBSTRINGS below.

# Path substrings that ALWAYS make a file trivial (override the dir walk).
# Each entry is checked in BOTH forms during classification (with and
# without the leading slash) so relative paths like `results/cache/foo.json`
# match the same as absolute `/Users/.../results/cache/foo.json`.
_ALWAYS_TRIVIAL_SUBSTRINGS = (
    "__pycache__/",
    "results/logs/",
    "results/log/",
    "results/cache/",
    ".venv/",
    "node_modules/",
)

# Extensions that ALWAYS make a file trivial.
_TRIVIAL_EXTENSIONS = (
    ".md",
    ".lock",
    ".html",
    ".log",
    ".bak",
    ".tmp",
)

# Specific filenames that are always trivial (lockfiles + memory index).
_TRIVIAL_FILENAMES = (
    "package-lock.json",
    "poetry.lock",
    "uv.lock",
    "MEMORY.md",   # memory index, not project code
)

# Extensions that count as non-trivial when no directory match was found
# during the walk. Used as a fallback so files at the project root (e.g.,
# pyproject.toml, setup.sh) still get coverage.
_NON_TRIVIAL_EXTENSIONS = (
    ".py",
    ".sh",
    ".sql",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
)


def _safe_exit_zero(*args, **kwargs):
    """Last-resort safety net: degrade to silent exit 0 on any error."""
    sys.exit(0)


def _read_stdin_json() -> dict:
    """Read JSON from stdin. Empty/malformed → empty dict."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except Exception:
        return {}


def _fresh_state(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "turn_started_at": time.time(),
        "edits": [],
        "codex_runs": [],
        # Per-path spec/plan doc tracking (Codex review 2026-05-03):
        #   key = repo-relative path (or whatever extractor returned)
        #   value = {"kind": "spec"|"plan", "touched_ts": float, "reviewed_ts": float|None}
        # touched_ts > reviewed_ts (or reviewed_ts is None) → needs review on Stop
        "spec_plan_docs": {},
    }


# ── Spec / plan classification ──────────────────────────────────────
#
# Spec and plan sheets are high-leverage docs whose freshness materially
# affects the implementation that follows. They live in two conventional
# directories under any project. Tracked separately from regular edits
# because (a) they're typically classified `_TRIVIAL_DIRS` for code-coverage
# purposes (docs/) but still need their own coengineering pass, and (b)
# we want per-path re-review semantics: if you touch the spec, codex
# review it, then touch it again, you should be reminded to re-review.
#
# Match conservatively. Only paths under `docs/superpowers/specs/` and
# `docs/superpowers/plans/` (anywhere in the path) ending in `.md`.

_SPEC_PATH_RE = re.compile(r"(?:^|/)docs/superpowers/specs/[\w./\-]+\.md$")
_PLAN_PATH_RE = re.compile(r"(?:^|/)docs/superpowers/plans/[\w./\-]+\.md$")


def _classify_spec_plan(file_path: str) -> str:
    """Return 'spec', 'plan', or '' if the path is neither."""
    if not file_path:
        return ""
    normalized = _strip_home_prefix(file_path)
    if _SPEC_PATH_RE.search(normalized):
        return "spec"
    if _PLAN_PATH_RE.search(normalized):
        return "plan"
    return ""


# Strict codex invocation detection — Codex finding: substring check
# false-positives on echo'd strings. Use a regex matching codex as a
# standalone command at start of statement with its first arg.
_CODEX_INVOKE_RE = re.compile(r"(?:^|[\s;&|])codex\s+\S+")


def _is_real_codex_invocation(command: str) -> bool:
    """True if the bash command actually invokes the codex CLI (not echoed)."""
    if not command:
        return False
    stripped = command.strip()
    if stripped.startswith(("echo ", "echo\t", "printf ", "printf\t",
                              "# ", "//")):
        return False
    return bool(_CODEX_INVOKE_RE.search(command))


# ── Prompt extraction & reviewed-file tracking (v2 hook tuning) ─────
#
# Goal: when the user runs `codex exec "review src/foo.py"` and then edits
# src/foo.py in response to Codex's findings, the hook should NOT fire on
# Stop. The original v1 logic only checked timestamps (codex run before
# vs after edit), which fired false positives on the very common
# "edit → codex review → fix → commit" workflow.
#
# v2 approach: extract file paths from the codex prompt body and remember
# them as "reviewed files" attached to the codex_run record. Edits to
# those files get covered even if they happened after the codex run.
#
# Conservative: better to miss a path than over-cover. Only matches paths
# under known project subdirectories with known file extensions.

# Union of all project directories the path extractor should recognize.
# v3: derived from the unified _NON_TRIVIAL_DIRS + _TRIVIAL_DIRS source so
# the extractor can never drift away from the classifier. Also includes
# 'results' explicitly because results/foo.json (calibration outputs) is
# meaningful even though we don't classify it via dir walk.
_PROJECT_DIRS = tuple(sorted(set(
    _NON_TRIVIAL_DIRS + _TRIVIAL_DIRS + ("results", "schema", "migrations")
)))
_PROJECT_DIRS_RE = "|".join(_PROJECT_DIRS)

# Negative look-behind: don't match paths embedded in URLs (preceded by
# `://` or any non-space char) or markdown link targets (preceded by `(`).
# Anchored to a real word boundary or whitespace/quote/backtick on the left.
_PROJECT_PATH_RE = re.compile(
    rf"(?<![/\w:.])((?:{_PROJECT_DIRS_RE})/[\w./\-]+\.(?:py|sh|sql|yaml|yml|toml|json|md|html|ts|tsx|js|jsx))"
)

# Codex review 2026-05-03: also extract absolute paths and normalize them to
# repo-relative form. The repo-relative regex (above) explicitly rejects
# paths preceded by `/` because of the URL/markdown-link guard. That same
# guard rejected legitimate `/Users/.../<project>/<dir>/file.ext` mentions
# in codex prompts, breaking spec/plan reviewed_ts updates.
#
# Strategy: a SECOND regex matches absolute paths whose tail includes a
# project-dir component, then `_normalize_to_repo_relative` strips the
# prefix down to the first project-dir component.
#
# Lookbehind `(?<![\w:/])` rejects URLs (`http://...` has `:` and `/` before
# path segments), embedded paths, and word-boundary leaks. Without these,
# a string like `https://github.com/org/repo/docs/X.md` would match
# `/org/repo/docs/X.md` and normalize to `docs/X.md` — false positive.
_ABSOLUTE_PROJECT_PATH_RE = re.compile(
    rf"(?<![\w:/])(/[\w./\-]+?/(?:{_PROJECT_DIRS_RE})/[\w./\-]+\.(?:py|sh|sql|yaml|yml|toml|json|md|html|ts|tsx|js|jsx))"
)


def _normalize_to_repo_relative(abs_path: str) -> str:
    """Given an absolute path, return the substring starting at the first
    known project-root dir component. ``/Users/x/Projects/p/src/foo.py``
    → ``src/foo.py``. Returns the input unchanged if no project dir found.
    """
    if not abs_path or not abs_path.startswith("/"):
        return abs_path
    parts = abs_path.split("/")
    for i, part in enumerate(parts):
        if part in _PROJECT_DIRS:
            return "/".join(parts[i:])
    return abs_path

# Cap to avoid pathological prompts blowing out the state file
_MAX_REVIEWED_FILES_PER_RUN = 50


# Command-substitution recognition: `$(cat <path>)` and `$(cat -- <path>)`.
# Single regex; the path group is greedy until whitespace or close-paren.
# Backticks are intentionally NOT supported — their quoting/escaping edge
# cases are worse and there's no real caller need yet (Codex Pass 8 review).
_CAT_SUB_RE = re.compile(r"\$\(\s*cat\s+(?:--\s+)?([^\s)]+)\s*\)")

# Caps to prevent unbounded growth of state if a caller accidentally
# substitutes a huge file or a `/dev/zero`-style non-regular file.
_CAT_MAX_BYTES_PER_FILE = 256 * 1024     # 256 KiB per file
_CAT_MAX_AGGREGATE_BYTES = 1024 * 1024   # 1 MiB total across all matches


def _expand_cat_substitutions(command: str) -> str:
    """Find ``$(cat <path>)`` substrings and return concatenated file
    contents up to ``_CAT_MAX_AGGREGATE_BYTES``.

    Safety per Codex Pass 8 review:
      - tilde expansion via ``os.path.expanduser``
      - regular-file check (rejects ``/dev/zero`` etc.)
      - per-file size cap (rejects files larger than ``_CAT_MAX_BYTES_PER_FILE``)
      - aggregate-bytes cap

    Unreadable matches are silently skipped. Returns ``""`` if no cat
    substitutions matched or all reads failed (caller falls through to the
    inline-arg branch).
    """
    # Codex Pass 11 fix: use the regex (which already handles whitespace
    # variants like `$( cat /path )`) for the early-out, NOT a literal
    # substring check. The literal `"$(cat" not in command` short-circuited
    # legitimate whitespace variants and returned "" before they could be
    # expanded.
    if not _CAT_SUB_RE.search(command):
        return ""

    parts: list[str] = []
    aggregate = 0
    for m in _CAT_SUB_RE.finditer(command):
        raw = m.group(1)
        try:
            path = Path(os.path.expanduser(raw))
            if not path.is_file():  # rejects /dev/zero, dirs, missing
                continue
            size = path.stat().st_size
            if size > _CAT_MAX_BYTES_PER_FILE:
                continue
            if aggregate + size > _CAT_MAX_AGGREGATE_BYTES:
                continue
            text = path.read_text(errors="replace")
            parts.append(text)
            aggregate += len(text.encode("utf-8", errors="replace"))
        except Exception:
            continue

    return "\n".join(parts) if parts else ""


def _extract_prompt_text(command: str) -> str:
    """Extract the codex prompt body from a bash command string.

    Handles four input shapes (checked in order):
      1. Heredoc body: ``codex exec << 'EOF' ... EOF``
      2. File redirect: ``codex exec < /tmp/prompt.txt`` (reads the file)
      3. Command substitution: ``codex exec "$(cat /tmp/p.md)"`` — read each
         cat'd file and concatenate their contents (Codex review 2026-05-03).
      4. Inline quoted arg: ``codex exec "the prompt text"``

    Returns the extracted prompt text, or the full command as a last-ditch
    fallback. All errors degrade to empty/whole-command — never raise.
    """
    if not command:
        return ""

    # 1. Heredoc — accepts `<<DELIM`, `<<-DELIM`, `<<'DELIM'`, `<<"DELIM"`,
    # delimiters with hyphens/underscores, and trailing text after the
    # closing delim. Heredoc syntax requires a newline after the delimiter,
    # so it can't false-positive on quoted in-prompt `<<` (which would all
    # be on a single line inside one quoted token).
    heredoc = re.search(
        r"<<-?\s*['\"]?([\w-]+)['\"]?\s*\n(.*?)\n\s*\1\b",
        command,
        re.DOTALL | re.MULTILINE,
    )
    if heredoc:
        return heredoc.group(2)

    # All remaining steps tokenize via shlex BEFORE looking for redirects
    # or substitutions. Token-level analysis is quote-aware: an in-prompt
    # `<` inside a quoted positional (e.g., 'a < b') stays bundled with
    # its prompt and never gets misread as a shell redirect.
    #
    # Codex Pass 14 finding: previously each step ran its own regex on the
    # RAW command, which made the same in-prompt-`<` mistake at step 2
    # (redirect detection) AND step 4 (positional extraction). Unified.
    try:
        import shlex
        try:
            parts = shlex.split(command)
        except ValueError:
            return command  # unbalanced quotes — fall through to whole-command regex
    except Exception:
        return command

    # Operators that consume the next token as their target.
    target_ops = {"<", "<<", "<<<", ">", ">>", "2>", "2>>", "&>", "&>>", "|"}
    # Self-contained operators (no target).
    self_ops = {"2>&1", "1>&2", "&>&1"}

    # Compact redirect splitter — shlex leaves `</tmp/p`, `>/tmp/p`,
    # `2>/tmp/p` as single tokens. Codex Pass 15 finding: the prior token
    # walker only matched whole-token `<`, so compact forms slipped through
    # as inline positionals and the redirect target was never read.
    #
    # Codex Pass 16 fix: alternation MUST list longest operators first.
    # Regex alternation is left-to-right, first-match-wins; with `<` listed
    # before `<<`, `<<EOF` would split as `<` + `<EOF` instead of `<<` +
    # `EOF`. Same trap for `>>`, `2>>`, `&>>`, `<<<`.
    _COMPACT_REDIR_RE = re.compile(r"^(<<<|<<|2>>|2>|&>>|&>|>>|>|<)([^\s].*)$")

    def _expand_compact_redirects(tokens: list[str]) -> list[str]:
        out: list[str] = []
        for t in tokens:
            m = _COMPACT_REDIR_RE.match(t)
            if m:
                out.append(m.group(1))
                out.append(m.group(2))
            else:
                out.append(t)
        return out

    parts = _expand_compact_redirects(parts)

    redirect_target: str | None = None
    positionals: list[str] = []
    i = 0
    while i < len(parts):
        t = parts[i]
        if t == "<" and i + 1 < len(parts):
            # First stdin redirect wins as the candidate prompt source.
            if redirect_target is None:
                redirect_target = parts[i + 1]
            i += 2
            continue
        if t in target_ops:
            i += 2  # skip operator + target
            continue
        if t in self_ops:
            i += 1
            continue
        positionals.append(t)
        i += 1

    # 2. Stdin file redirect (token-level). Try reading the redirect target;
    # fall through to later steps if it's empty, /dev/null, or unreadable.
    if redirect_target:
        try:
            content = Path(redirect_target).read_text()
        except Exception:
            content = ""
        if content and redirect_target != "/dev/null":
            return content

    # 3. Command substitution `$(cat <path>)` — the substitution lives
    # INSIDE a quoted positional, which shlex preserves as one token. Run
    # the regex on the original command (which is equivalent to running
    # it on the joined positionals — but cheaper). Same Pass 9-11 commit-
    # to-cat semantics: if `$(cat ...)` appears, return the expanded
    # content (or "") and never fall through to the inline-arg branch.
    if _CAT_SUB_RE.search(command):
        return _expand_cat_substitutions(command)

    # 4. Inline quoted prompt — pick the longest non-flag, non-keyword
    # positional argument from the cleaned token list (redirects already
    # removed above).
    non_flag = [
        p for p in positionals
        if p and not p.startswith("-") and p not in ("codex", "exec")
    ]
    if non_flag:
        return max(non_flag, key=len)

    return command


_URL_RE = re.compile(r"\bhttps?://\S+")


def _extract_reviewed_files(command: str) -> list[str]:
    """Extract repo-relative file paths from a codex prompt.

    Returns up to _MAX_REVIEWED_FILES_PER_RUN unique paths matching the
    conservative project-path regex. Also extracts absolute paths and
    normalizes them to repo-relative form (Codex review 2026-05-03).

    URL hardening (Codex Pass 4 review): URLs are stripped from the prompt
    BEFORE path extraction. Lookbehind alone could not protect against
    URL query/fragment leaks like `?file=src/foo.py` or `#docs/specs/X.md`
    where the char before the path is `=` or `#` (not in any reasonable
    rejection set). Pre-stripping URLs is the robust fix.
    """
    prompt = _extract_prompt_text(command)
    if not prompt:
        return []

    prompt = _URL_RE.sub(" ", prompt)

    seen: set[str] = set()
    out: list[str] = []

    def _add(path: str) -> bool:
        if not path or path.endswith("/.") or path.endswith("/") or "//" in path:
            return True  # invalid; continue
        if path in seen:
            return True
        seen.add(path)
        out.append(path)
        return len(out) < _MAX_REVIEWED_FILES_PER_RUN

    # 1. Repo-relative paths (existing extraction)
    for m in _PROJECT_PATH_RE.finditer(prompt):
        if not _add(m.group(1)):
            return out

    # 2. Absolute paths normalized to repo-relative
    for m in _ABSOLUTE_PROJECT_PATH_RE.finditer(prompt):
        normalized = _normalize_to_repo_relative(m.group(1))
        if not _add(normalized):
            return out

    return out


def _edit_is_covered_by_reviewed(edit_path: str, reviewed: set[str]) -> bool:
    """True if an edit's file path matches any reviewed repo-relative path.

    Strategy:
      1. Exact match (handles relative paths)
      2. Suffix match: edit_path endswith ('/' + reviewed_path)
         e.g., reviewed='src/rotation/regime_v4.py' matches
         edit='/Users/example/Projects/sample_project/src/rotation/regime_v4.py'

    Suffix match is robust to project-root differences between where Codex
    was called and where the edit happened.
    """
    if not edit_path or not reviewed:
        return False
    if edit_path in reviewed:
        return True
    for r in reviewed:
        if edit_path.endswith("/" + r):
            return True
    return False


def _strip_home_prefix(file_path: str) -> str:
    """Strip ``$HOME/`` prefix from an absolute path so the subsequent walk
    starts at the user's home-relative path.

    Codex round-3 finding (2026-04-08): walking the raw absolute path allows
    ancestor directory names (e.g., ``/tmp/tests/...``) to contaminate
    classification. Stripping the home prefix handles the common
    ``/Users/<name>/Projects/<proj>/src/...`` case cleanly. Falls back to
    the original path if no home prefix match.
    """
    try:
        home = str(Path.home())
        if file_path.startswith(home + "/"):
            return file_path[len(home) + 1:]
    except Exception:
        pass
    return file_path


def _is_non_trivial(file_path: str) -> bool:
    """True if this file should trigger a codex coengineering reminder.

    Walks the path components left-to-right per the v3 root-dir semantic.
    See _NON_TRIVIAL_DIRS / _TRIVIAL_DIRS docstring for the full algorithm.

    The path is first stripped of any ``$HOME/`` prefix so the walk starts
    as close to the project root as possible. This handles the common
    macOS/Linux case where edits arrive as ``/Users/<name>/...`` absolute
    paths. For paths outside home (e.g., /tmp/foo, /var/bar), this is a
    no-op and the walk operates on the raw path — which is fine since
    Claude Code rarely edits such paths in practice.
    """
    if not file_path:
        return False

    # Normalize: strip $HOME prefix so ancestor noise (e.g., /tmp/tests/...)
    # can't contaminate the walk for home-relative paths.
    normalized = _strip_home_prefix(file_path)

    # 1. Always-trivial substrings win. Each substring is checked with AND
    #    without a leading slash so relative paths match the same as
    #    absolute paths. E.g., `results/cache/x.json` and `/some/abs/path/
    #    results/cache/x.json` both match `results/cache/`.
    for sub in _ALWAYS_TRIVIAL_SUBSTRINGS:
        if sub in normalized or ("/" + sub) in normalized:
            return False

    # 2. Always-trivial extensions win
    for ext in _TRIVIAL_EXTENSIONS:
        if normalized.endswith(ext):
            return False

    # 3. Specific always-trivial filenames win
    basename = normalized.rsplit("/", 1)[-1]
    if basename in _TRIVIAL_FILENAMES:
        return False

    # 4. Walk path components left-to-right; first project-root dir wins.
    #    After _strip_home_prefix, the walk typically starts at something
    #    like `Projects/sample_project/src/...` — the first matching
    #    `src` wins. This is the root-dir semantic: `tools/tests/foo.py`
    #    is classified as non-trivial (under tools/) because `tools`
    #    appears before `tests` in the walk.
    parts = normalized.split("/")
    for part in parts:
        if part in _NON_TRIVIAL_DIRS:
            return True
        if part in _TRIVIAL_DIRS:
            return False

    # 5. No project dir matched — fall back to extension classification.
    #    This catches root-level project files like pyproject.toml or setup.sh.
    for ext in _NON_TRIVIAL_EXTENSIONS:
        if normalized.endswith(ext):
            return True

    return False


# ── Hook subcommand handlers ─────────────────────────────────────


def cmd_reset_turn() -> int:
    """UserPromptSubmit: reset per-turn state."""
    data = _read_stdin_json()
    sid = data.get("session_id", "")
    _debug_log(f"reset-turn sid={sid}")
    if not sid:
        return 0
    with _locked_state(sid) as (_state, save):
        save(_fresh_state(sid))
    return 0


def cmd_record_edit() -> int:
    """PostToolUse Edit|Write|MultiEdit: append touched file."""
    data = _read_stdin_json()
    sid = data.get("session_id", "")
    tool_input = data.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path") or ""
    # MultiEdit may use 'file_path' or 'path'; be lenient
    if not file_path:
        file_path = tool_input.get("path", "")

    if not sid or not file_path:
        return 0

    nt = _is_non_trivial(file_path)
    sp_kind = _classify_spec_plan(file_path)
    _debug_log(f"record-edit sid={sid} file={file_path} non_trivial={nt} sp_kind={sp_kind!r}")

    now = time.time()

    with _locked_state(sid) as (state, save):
        state["edits"].append({
            "file": file_path,
            "tool": data.get("tool_name", "unknown"),
            "ts": now,
            "non_trivial": nt,
        })
        # Spec/plan tracking — independent of nt_edits flow. We always
        # bump touched_ts on a touch; reviewed_ts is set only by record-codex.
        if sp_kind:
            sp_docs = state.setdefault("spec_plan_docs", {})
            existing = sp_docs.get(file_path)
            if existing:
                existing["touched_ts"] = now
                existing["kind"] = sp_kind  # tolerate path moving across kinds
            else:
                sp_docs[file_path] = {
                    "kind": sp_kind,
                    "touched_ts": now,
                    "reviewed_ts": None,
                }
        save(state)
    return 0


def cmd_record_codex() -> int:
    """PostToolUse Bash: if command actually invokes codex CLI, record it.

    Also extracts file paths mentioned in the codex prompt and attaches
    them to the run record. The Stop check unions reviewed_files across
    all codex_runs in the turn and treats matching edits as covered.
    """
    data = _read_stdin_json()
    sid = data.get("session_id", "")
    tool_input = data.get("tool_input", {}) or {}
    command = tool_input.get("command", "") or ""

    if not sid or not command:
        return 0

    if not _is_real_codex_invocation(command):
        return 0

    reviewed_files = _extract_reviewed_files(command)
    _debug_log(
        f"record-codex sid={sid} reviewed={len(reviewed_files)} files "
        f"cmd={command[:80]}"
    )

    now = time.time()

    with _locked_state(sid) as (state, save):
        state["codex_runs"].append({
            "ts": now,
            "cmd_preview": command[:200],
            "reviewed_files": reviewed_files,
        })
        # If this codex run mentioned any tracked spec/plan path (exact or
        # suffix match), update its reviewed_ts so the Stop check knows
        # it's been re-reviewed since the last touch.
        sp_docs = state.setdefault("spec_plan_docs", {})
        if sp_docs and reviewed_files:
            reviewed_set = set(reviewed_files)
            for tracked_path, doc in sp_docs.items():
                if _edit_is_covered_by_reviewed(tracked_path, reviewed_set):
                    doc["reviewed_ts"] = now
        save(state)
    return 0


def cmd_check_stop() -> int:
    """Stop: decide whether to block once with a coengineering reminder."""
    data = _read_stdin_json()
    sid = data.get("session_id", "")
    stop_hook_active = data.get("stop_hook_active", False)

    _debug_log(f"check-stop sid={sid} stop_hook_active={stop_hook_active}")

    # Loop guard: per Claude Code hooks docs, if stop_hook_active is True
    # we've already blocked once this stop sequence. Exit 0 to allow stop.
    if stop_hook_active:
        return 0

    if not sid:
        return 0

    # Read state under lock so we don't see a torn state mid-write
    with _locked_state(sid) as (state, _save):
        pass

    # ── Section 1: code-phase coverage (existing logic) ─────────────
    nt_edits = [e for e in state.get("edits", []) if e.get("non_trivial")]
    codex_runs = state.get("codex_runs", [])

    # Union all reviewed_files across the turn's codex runs.
    reviewed_files: set[str] = set()
    for run in codex_runs:
        for f in run.get("reviewed_files", []) or []:
            reviewed_files.add(f)

    uncovered_edits = [
        e for e in nt_edits
        if not _edit_is_covered_by_reviewed(e.get("file", ""), reviewed_files)
    ]

    code_section = ""
    if uncovered_edits:
        files = sorted({e["file"] for e in uncovered_edits})
        files_short = []
        for f in files[:8]:
            parts = f.split("/")
            files_short.append("/".join(parts[-3:]) if len(parts) >= 3 else f)
        if len(files) > 8:
            files_short.append(f"... +{len(files) - 8} more")

        coverage_note = ""
        if reviewed_files:
            coverage_note = (
                f"\n  Note: {len(nt_edits) - len(uncovered_edits)} other edit(s) in "
                f"this turn ARE covered by previously-reviewed files "
                f"({len(reviewed_files)} files in codex prompts)."
            )
        code_section = (
            f"## Code phase — {len(files)} non-trivial file(s) without codex review\n\n"
            f"Files: {', '.join(files_short)}{coverage_note}\n\n"
            f"Run codex exec on these files. Suggested prompt template:\n"
            f"  ~/.claude/codex-prompts/phase-review.md\n"
        )

    # ── Section 2: spec/plan freshness coverage (new) ───────────────
    sp_docs = state.get("spec_plan_docs", {}) or {}
    needs_review: list[tuple[str, dict]] = [
        (path, doc) for path, doc in sp_docs.items()
        if doc.get("touched_ts") and (
            doc.get("reviewed_ts") is None
            or doc["touched_ts"] > doc["reviewed_ts"]
        )
    ]

    spec_plan_section = ""
    if needs_review:
        spec_paths = [p for p, d in needs_review if d.get("kind") == "spec"]
        plan_paths = [p for p, d in needs_review if d.get("kind") == "plan"]

        def _shorten(paths):
            out = []
            for f in paths[:6]:
                parts = f.split("/")
                out.append("/".join(parts[-3:]) if len(parts) >= 3 else f)
            if len(paths) > 6:
                out.append(f"... +{len(paths) - 6} more")
            return out

        lines = []
        if spec_paths:
            lines.append(
                f"Spec sheet(s) touched without codex review:\n  "
                + ", ".join(_shorten(spec_paths))
                + "\n  Suggested prompt: ~/.claude/codex-prompts/spec-review.md"
            )
        if plan_paths:
            lines.append(
                f"Plan sheet(s) touched without codex review:\n  "
                + ", ".join(_shorten(plan_paths))
                + "\n  Suggested prompt: ~/.claude/codex-prompts/plan-review.md"
            )
        spec_plan_section = (
            f"## Spec/plan — {len(needs_review)} doc(s) need codex review\n\n"
            + "\n\n".join(lines)
            + "\n"
        )

    if not code_section and not spec_plan_section:
        _debug_log(
            f"check-stop sid={sid} all coverage met — skipping block"
        )
        return 0

    reason_parts = ["⚠️  Codex coengineering reminder (memory: feedback_codex_coengineering.md):", ""]
    if code_section:
        reason_parts.append(code_section)
    if spec_plan_section:
        reason_parts.append(spec_plan_section)
    reason_parts.append(
        "Per the project rule, run codex exec for second-source review before "
        "the user commits. Capture pattern (see ~/.claude/CLAUDE.md):\n"
        "  codex exec --skip-git-repo-check -c model_reasoning_effort=xhigh \\\n"
        "      \"$(cat <prompt-template>)\" > /tmp/codex.log 2>&1 < /dev/null\n\n"
        "If this work is genuinely trivial enough to skip Codex, say so "
        "explicitly in your next response and the user can decide. "
        "Single-fire reminder — won't block again on this stop sequence."
    )

    # Block once via JSON output
    print(json.dumps({
        "decision": "block",
        "reason": "\n".join(reason_parts),
    }))
    return 0


# ── Entrypoint ───────────────────────────────────────────────────


SUBCOMMANDS = {
    "reset-turn": cmd_reset_turn,
    "record-edit": cmd_record_edit,
    "record-codex": cmd_record_codex,
    "check-stop": cmd_check_stop,
}


def main() -> int:
    # Bullet-proof: any uncaught exception → silent exit 0
    sys.excepthook = lambda *a, **k: _safe_exit_zero()

    if len(sys.argv) < 2:
        return 0
    subcmd = sys.argv[1]
    handler = SUBCOMMANDS.get(subcmd)
    if not handler:
        return 0
    try:
        return handler()
    except Exception:
        return 0  # telemetry, not enforcement


if __name__ == "__main__":
    sys.exit(main())
