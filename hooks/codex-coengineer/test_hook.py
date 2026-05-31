"""Tests for the codex-coengineer hook.

Run with:  python tools/claude-code-hooks/codex-coengineer/test_hook.py
or:        pytest tools/claude-code-hooks/codex-coengineer/test_hook.py -v

Focuses on the prompt-extraction surface — the area that has been the source
of every Needs-Revision verdict in Codex Pass 1-5 reviews on 2026-05-03.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

# Load the hook module by file path so the test doesn't depend on import-path
# configuration (the hook lives outside src/ and isn't a package).
_HOOK_PATH = Path(__file__).parent / "hook.py"
_spec = importlib.util.spec_from_file_location("codex_coengineer_hook", _HOOK_PATH)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


# ── _extract_reviewed_files: URL-handling regression tests ──────────────


URL_LEAK_CASES = [
    # Codex Pass 4 — query-string + fragment URL leaks
    ('review https://x.com/?file=docs/superpowers/specs/X.md', []),
    ('review https://x.com/#docs/superpowers/specs/X.md',     []),
    ('review https://x.com/?next=src/foo.py',                 []),
    # Codex Pass 3 — path-segment URL leaks
    ('review https://github.com/org/repo/docs/superpowers/specs/X.md', []),
    ('review http://example.com/src/foo.py',                  []),
    # Markdown link with URL target
    ('see [the spec](https://x.com/docs/specs/Y.md)',         []),
]


REAL_PATH_CASES = [
    # Repo-relative
    ('review docs/superpowers/specs/X.md',
     ['docs/superpowers/specs/X.md']),
    # Absolute path under user home
    ('review /Users/example/Projects/sample_project/docs/superpowers/specs/X.md',
     ['docs/superpowers/specs/X.md']),
    # Absolute path with project-dir name in tail (non-temp prefix; /tmp is now
    # special-cased as scratch — see the temp-exclusion cases below)
    ('review /opt/ci/checkout/src/foo.py',                     ['src/foo.py']),
    # User dir name CONTAINS "src" but not as path component
    ('review /Users/user-with-src-in-name/x.py',               []),
    # Markdown link with relative target
    ('see [relative](docs/superpowers/specs/X.md)',
     ['docs/superpowers/specs/X.md']),
    # 2026-05-31 — hooks/ credited so codex reviews of the hook itself count
    ('review hooks/codex-coengineer/hook.py',
     ['hooks/codex-coengineer/hook.py']),
    ('review /Users/example/Projects/sample_project/hooks/codex-coengineer/hook.py',
     ['hooks/codex-coengineer/hook.py']),
    # 2026-05-31 (Codex verify) — temp absolute paths must NOT credit real repo files
    ('review /tmp/src/foo.py',                                 []),
    ('review /tmp/docs/superpowers/specs/X.md',                []),
]


def _check(case_set: list[tuple[str, list[str]]], label: str) -> tuple[int, int]:
    passed = 0
    for prompt, expected in case_set:
        got = hook._extract_reviewed_files(prompt)
        ok = sorted(got) == sorted(expected)
        if ok:
            passed += 1
        print(f"  {'✅' if ok else '❌'} {label}: {prompt[:60]!r}")
        if not ok:
            print(f"      got:      {got}")
            print(f"      expected: {expected}")
    return passed, len(case_set)


# ── _classify_spec_plan ────────────────────────────────────────────────


SPEC_PLAN_CASES = [
    ('docs/superpowers/specs/X.md', 'spec'),
    ('docs/superpowers/plans/X.md', 'plan'),
    ('/Users/x/Projects/p/docs/superpowers/specs/X.md', 'spec'),
    ('docs/random.md',              ''),
    ('src/foo.py',                  ''),
    ('docs/superpowers/specs/foo.txt', ''),  # wrong extension
]


# ── _expand_cat_substitutions: $(cat /path) handling ───────────────────


def test_cat_substitution_single_file(tmp_path):
    """Single $(cat /path) reads the file's contents."""
    p = tmp_path / "prompt.md"
    p.write_text("review docs/superpowers/specs/X.md please")
    cmd = f'codex exec "$(cat {p})"'
    text = hook._extract_prompt_text(cmd)
    assert "review docs/superpowers/specs/X.md please" in text
    # And path extraction works through it
    assert hook._extract_reviewed_files(cmd) == ['docs/superpowers/specs/X.md']


def test_cat_substitution_multiple_files(tmp_path):
    """Two $(cat ...) → contents concatenated."""
    p1 = tmp_path / "a.md"; p1.write_text("review src/foo.py")
    p2 = tmp_path / "b.md"; p2.write_text("and src/bar.py")
    cmd = f'codex exec "$(cat {p1}) and $(cat {p2})"'
    files = hook._extract_reviewed_files(cmd)
    assert sorted(files) == ['src/bar.py', 'src/foo.py']


def test_cat_substitution_missing_file_falls_through(tmp_path):
    """Missing file → silently skipped; if no readable cats, fall through to inline."""
    nonexistent = tmp_path / "nope.md"
    cmd = f'codex exec "$(cat {nonexistent})"'
    # No readable cats → returns ""; downstream falls through to whole-command
    text = hook._extract_prompt_text(cmd)
    # Either empty (cat returned nothing) OR the inline parser picks the literal
    # `$(cat /tmp/.../nope.md)` arg. Either way no real path extraction:
    assert hook._extract_reviewed_files(cmd) == []


def test_cat_substitution_oversize_rejected(tmp_path):
    """File > _CAT_MAX_BYTES_PER_FILE is rejected."""
    big = tmp_path / "huge.md"
    big.write_text("x" * (hook._CAT_MAX_BYTES_PER_FILE + 1))
    cmd = f'codex exec "$(cat {big})"'
    # Content rejected → no fallback to inline → no path extraction
    assert hook._extract_reviewed_files(cmd) == []


def test_cat_substitution_silent_pass_missing_project_path():
    """Codex Pass 9: rejected $(cat /tmp/src/missing.py) must NOT extract
    `src/missing.py` from the literal substitution token via shlex fallback.
    """
    cmd = 'codex exec "$(cat /tmp/src/missing.py)"'
    assert hook._extract_reviewed_files(cmd) == []


def test_cat_substitution_silent_pass_missing_spec_path():
    """Same silent-pass concern with a spec/plan-shaped path."""
    cmd = 'codex exec "$(cat /tmp/docs/superpowers/specs/missing.md)"'
    assert hook._extract_reviewed_files(cmd) == []


def test_cat_substitution_silent_pass_oversize(tmp_path):
    """Oversize cat'd file with project-dir tail must NOT register the path."""
    big = tmp_path / "src" / "big.py"
    big.parent.mkdir()
    big.write_text("x" * (hook._CAT_MAX_BYTES_PER_FILE + 1))
    cmd = f'codex exec "$(cat {big})"'
    assert hook._extract_reviewed_files(cmd) == []


def test_cat_substitution_whitespace_variant_silent_pass():
    """Codex Pass 10: $( cat /tmp/src/missing.py ) (with whitespace inside
    the substitution) must trigger the same commit-to-cat guard as $(cat ...).
    Regression: literal substring check `'$(cat' in command` missed this.
    """
    cmd = 'codex exec "$( cat /tmp/src/missing.py )"'
    assert hook._extract_reviewed_files(cmd) == []
    cmd2 = 'codex exec "$(  cat   /tmp/docs/superpowers/specs/Y.md  )"'
    assert hook._extract_reviewed_files(cmd2) == []


def test_dev_null_redirect_falls_through_to_inline():
    """Codex Pass 13: `codex exec "...prompt with paths..." < /dev/null` is
    the canonical pattern after the stdin-block CLAUDE.md fix. /dev/null
    must NOT be treated as the prompt source; extraction must fall through
    to the inline positional arg.
    """
    cmd = 'codex exec "review src/foo.py" < /dev/null'
    assert hook._extract_reviewed_files(cmd) == ['src/foo.py']


def test_empty_file_redirect_falls_through_to_inline(tmp_path):
    """Empty redirect file → fall through to inline (same semantic as /dev/null)."""
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    cmd = f'codex exec "review src/foo.py" < {empty}'
    assert hook._extract_reviewed_files(cmd) == ['src/foo.py']


def test_nonempty_redirect_still_wins_over_inline(tmp_path):
    """Non-empty < file redirect must still take precedence over inline arg."""
    real_prompt = tmp_path / "real.md"
    real_prompt.write_text("review src/from_redirect.py")
    cmd = f'codex exec "review src/from_inline.py" < {real_prompt}'
    files = hook._extract_reviewed_files(cmd)
    assert 'src/from_redirect.py' in files
    assert 'src/from_inline.py' not in files


def test_in_prompt_lt_not_misread_as_redirect():
    """Codex Pass 13: a literal `<` inside a QUOTED prompt (e.g., 'a < b in
    src/foo.py') must not be treated as a shell redirect. Token-level
    filtering preserves quoted content; only standalone redirect ops are
    dropped along with their targets.
    """
    cmd = 'codex exec "review whether a < b in src/foo.py" < /dev/null'
    assert hook._extract_reviewed_files(cmd) == ['src/foo.py']
    # Even with a longer redirect target, the prompt stays intact.
    cmd2 = 'codex exec "review whether a < b in src/foo.py" < /tmp/very/long/path/empty.txt'
    # If the target file doesn't exist or is empty, fall through; in-prompt
    # `<` is preserved; only the unquoted redirect operator is removed.
    assert 'src/foo.py' in hook._extract_reviewed_files(cmd2)


def test_inline_redirect_target_not_extracted():
    """Codex Pass 13 regression: `< /tmp/src/empty.py` redirect target must
    NOT be path-extracted via shlex fallback when the redirect file is
    empty/non-existent.
    """
    cmd = 'codex exec "review src/foo.py" < /tmp/src/missing-empty.py'
    files = hook._extract_reviewed_files(cmd)
    assert 'src/foo.py' in files
    assert 'src/missing-empty.py' not in files


def test_stderr_merge_2gt_amp1_dropped(tmp_path):
    """`2>&1` is a self-contained redirect (no target) — must not be picked
    as a positional argument."""
    cmd = 'codex exec "review src/foo.py" 2>&1'
    assert hook._extract_reviewed_files(cmd) == ['src/foo.py']


def test_step2_redirect_detection_quote_aware():
    """Codex Pass 14: step 2's `< /path` detection must be quote-aware too,
    not just step 4. A quoted prompt like 'review whether a < pyproject.toml
    in src/foo.py' previously had its `< pyproject.toml` snippet matched as
    a real redirect; pyproject.toml's contents were returned as the prompt,
    and src/foo.py was never extracted.
    """
    cmd = 'codex exec "review whether a < pyproject.toml in src/foo.py"'
    files = hook._extract_reviewed_files(cmd)
    assert files == ['src/foo.py']


def test_step2_redirect_inside_prompt_with_real_stdin_redirect():
    """The same in-prompt `<` plus a real stdin redirect: only the unquoted
    redirect operator should be honored; in-prompt `<` stays inside the
    positional and the path inside it is extracted.
    """
    cmd = 'codex exec "review whether a < pyproject.toml in src/foo.py" < /dev/null'
    files = hook._extract_reviewed_files(cmd)
    assert files == ['src/foo.py']


def test_compact_redirect_form_extraction(tmp_path):
    """Codex Pass 15: shell-compact `</tmp/p` (no space between `<` and the
    path) must be split into operator + target tokens. Otherwise the
    prompt file isn't read and inline-positional fallback can't find anything.
    """
    p = tmp_path / "prompt.md"
    p.write_text("review src/from_compact.py")
    cmd = f'codex exec </{p}'
    # Note: shlex sees `</...` as one token. The expander must split it.
    text = hook._extract_prompt_text(cmd)
    assert "review src/from_compact.py" in text
    assert hook._extract_reviewed_files(cmd) == ['src/from_compact.py']


def test_compact_dev_null_redirect_falls_through():
    """Compact `</dev/null` must also fall through to inline (same semantic
    as `< /dev/null`)."""
    cmd = 'codex exec "review src/foo.py" </dev/null'
    assert hook._extract_reviewed_files(cmd) == ['src/foo.py']


def test_compact_stdout_redirect_dropped():
    """`>/tmp/log` (compact stdout redirect) must not be picked as a positional."""
    cmd = 'codex exec "review src/foo.py" >/tmp/log 2>&1 </dev/null'
    assert hook._extract_reviewed_files(cmd) == ['src/foo.py']


def test_compact_long_redirect_operators_split_correctly():
    """Codex Pass 16: longer operators (>>, 2>>, &>>, <<<) must split before
    shorter ones (>, 2>, &>, <). Regex alternation is left-to-right; if
    ordered shortest-first, `>>file` would be split as `>` + `>file` and the
    target token would start with `>`, breaking the walker.
    """
    # `>>file` should split to `>>` + `file` and the file should be dropped
    cmd1 = 'codex exec "review src/foo.py" >>/tmp/append.log'
    assert hook._extract_reviewed_files(cmd1) == ['src/foo.py']

    # `2>>file` should split to `2>>` + `file`
    cmd2 = 'codex exec "review src/foo.py" 2>>/tmp/err.log'
    assert hook._extract_reviewed_files(cmd2) == ['src/foo.py']

    # `&>>file` should split to `&>>` + `file`
    cmd3 = 'codex exec "review src/foo.py" &>>/tmp/all.log'
    assert hook._extract_reviewed_files(cmd3) == ['src/foo.py']


def test_cat_substitution_whitespace_variant_readable(tmp_path):
    """Codex Pass 11: $( cat <readable> ) (whitespace variant pointing to a
    real file) must extract the file's contents — not return empty due to
    a stale literal substring guard inside _expand_cat_substitutions.
    """
    p = tmp_path / "prompt.md"
    p.write_text("review src/foo.py and docs/superpowers/plans/Z.md")
    cmd = f'codex exec "$( cat {p} )"'
    files = hook._extract_reviewed_files(cmd)
    assert sorted(files) == ['docs/superpowers/plans/Z.md', 'src/foo.py']


def test_cat_substitution_non_regular_rejected():
    """/dev/zero is not a regular file — must be rejected."""
    cmd = 'codex exec "$(cat /dev/zero)"'
    text = hook._expand_cat_substitutions(cmd)
    assert text == ""


def test_cat_substitution_tilde_expansion(tmp_path, monkeypatch):
    """Tilde paths are expanded via os.path.expanduser."""
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / "p.md"
    p.write_text("review docs/superpowers/plans/Y.md")
    cmd = 'codex exec "$(cat ~/p.md)"'
    files = hook._extract_reviewed_files(cmd)
    assert files == ['docs/superpowers/plans/Y.md']


def test_cat_substitution_precedence_below_heredoc(tmp_path):
    """Heredoc takes precedence over $(cat ...)."""
    p = tmp_path / "p.md"
    p.write_text("review src/cat-version.py")
    cmd = (f'codex exec "$(cat {p})" << EOF\n'
           f'review src/heredoc-version.py\n'
           f'EOF')
    files = hook._extract_reviewed_files(cmd)
    # heredoc body should win
    assert 'src/heredoc-version.py' in files
    # ensure cat version did NOT also leak in (precedence is exclusive)
    assert 'src/cat-version.py' not in files


def test_cat_substitution_precedence_below_redirect(tmp_path):
    """File redirect (< /path) takes precedence over $(cat ...)."""
    p_redir = tmp_path / "redir.md"
    p_redir.write_text("review src/redir-version.py")
    p_cat = tmp_path / "cat.md"
    p_cat.write_text("review src/cat-version.py")
    cmd = f'codex exec "$(cat {p_cat})" < {p_redir}'
    files = hook._extract_reviewed_files(cmd)
    assert files == ['src/redir-version.py']


# ── pytest-style + standalone runner ───────────────────────────────────


def test_url_leak_cases():
    p, n = _check(URL_LEAK_CASES, "url-leak")
    assert p == n, f"{n - p} URL-leak case(s) failed"


def test_real_path_cases():
    p, n = _check(REAL_PATH_CASES, "real-path")
    assert p == n, f"{n - p} real-path case(s) failed"


def test_spec_plan_classifier():
    fails = []
    for path, expected in SPEC_PLAN_CASES:
        got = hook._classify_spec_plan(path)
        ok = got == expected
        if not ok:
            fails.append((path, got, expected))
        print(f"  {'✅' if ok else '❌'} classify: {path!r} → {got!r}")
    assert not fails, f"classifier failed on: {fails}"


def test_hooks_dir_extractor_only_not_classifier():
    """`hooks` is extractor-only (Codex stop-gate finding 2026-05-31): codex
    reviews of hook source files must be credited, but `hooks` must NOT be a
    classifier dir — that would mark EXTENSIONLESS hook files (e.g.
    hooks/pre-commit) non-trivial while the extension-required extractor could
    never credit them, a permanent uncoverable false positive."""
    # Extractor credits reviews of hook source files (they have extensions)
    assert hook._extract_reviewed_files(
        'review hooks/codex-coengineer/hook.py'
    ) == ['hooks/codex-coengineer/hook.py']
    # .py / .sh hook files stay non-trivial via the extension fallback
    assert hook._is_non_trivial('hooks/codex-coengineer/hook.py') is True
    assert hook._is_non_trivial('hooks/setup.sh') is True
    # Extensionless hook files stay TRIVIAL — no uncoverable false positive
    assert hook._is_non_trivial('hooks/pre-commit') is False
    assert hook._is_non_trivial('hooks/post-merge') is False


def test_system_temp_files_are_trivial():
    """System-temp scratch (e.g. /tmp/foo.py) is ephemeral and never committed,
    so it must be trivial — but a project's own tmp/ subdir must NOT be
    over-matched. The classifier uses a FIXED system-root allowlist, never
    $TMPDIR, so a custom TMPDIR pointing inside a repo cannot over-trivialize real
    files. (Soft-hook false-fire finding + Codex verify, 2026-05-31.)"""
    # Absolute paths under known system temp roots -> trivial
    assert hook._is_non_trivial('/tmp/mk_proposal.py') is False
    assert hook._is_non_trivial('/private/tmp/scratch.py') is False
    assert hook._is_non_trivial('/var/tmp/scratch.py') is False
    assert hook._is_non_trivial('/var/folders/ab/cd/T/scratch.py') is False  # macOS default
    # spec/plan under temp also de-classified (consistent guard)
    assert hook._classify_spec_plan('/tmp/docs/superpowers/specs/X.md') == ''
    # A project's own tmp/ subdir is NOT over-matched -> stays non-trivial
    # (bulletproof: the impl uses a fixed allowlist and never reads $TMPDIR)
    assert hook._is_non_trivial(
        '/Users/example/Projects/sample_project/tmp/real.py'
    ) is True
    # relative + real code paths unaffected
    assert hook._is_non_trivial('src/foo.py') is True


if __name__ == "__main__":
    print("=== URL-leak regression ===")
    p1, n1 = _check(URL_LEAK_CASES, "url-leak")
    print("\n=== Real-path extraction ===")
    p2, n2 = _check(REAL_PATH_CASES, "real-path")
    print("\n=== Spec/plan classifier ===")
    test_spec_plan_classifier()
    total_p = p1 + p2
    total_n = n1 + n2
    print(f"\nExtractor: {total_p}/{total_n} pass")
