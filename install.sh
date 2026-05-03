#!/usr/bin/env bash
#
# Install claude-code-coengineer into ~/.claude/.
#
# Idempotent: re-running this script is safe. The hooks block in
# ~/.claude/settings.json is merged (not duplicated). Existing files are
# backed up to ~/.claude/.coengineer-backup-<timestamp>/ before being
# replaced.
#
# Usage:
#   ./install.sh                  # install / update
#   ./install.sh --dry-run        # show what would happen, change nothing
#   ./install.sh --force          # don't prompt to back up
#
set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${HOME}/.claude"
HOOK_DIR="${CLAUDE_DIR}/hooks/codex-coengineer"
PROMPTS_DIR="${CLAUDE_DIR}/codex-prompts"
SETTINGS_FILE="${CLAUDE_DIR}/settings.json"
BACKUP_DIR="${CLAUDE_DIR}/.coengineer-backup-$(date +%Y%m%d-%H%M%S)"

DRY_RUN=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --force)   FORCE=1 ;;
        --help|-h)
            sed -n '3,10p' "$0" | sed 's/^# //;s/^#$//'
            exit 0
            ;;
    esac
done

say() {
    printf '%s %s\n' "$1" "${*:2}"
}
run() {
    if [ "$DRY_RUN" = "1" ]; then
        say "  [dry-run]" "$*"
    else
        eval "$@"
    fi
}

# ── Pre-flight checks ────────────────────────────────────────────────
say "→" "Pre-flight checks"

if [ ! -d "$CLAUDE_DIR" ]; then
    echo "  ❌ ${CLAUDE_DIR} does not exist. Is Claude Code installed?" >&2
    exit 1
fi
say "  ✓" "${CLAUDE_DIR} exists"

if ! command -v python3 >/dev/null 2>&1; then
    echo "  ❌ python3 not found. Install Python 3.10+." >&2
    exit 1
fi
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
say "  ✓" "python3 ${PY_VERSION}"

if ! command -v codex >/dev/null 2>&1; then
    echo "  ⚠️  codex CLI not found. Install with:" >&2
    echo "      npm install -g @openai/codex" >&2
    echo "      codex login" >&2
    echo "  Continuing install — the hook will work but coengineering won't." >&2
fi

# python jq-equivalent for JSON merging — we use python's stdlib so we
# don't add a jq dependency on macOS users without homebrew.
PYTHON_JSON_MERGE='
import json, sys, pathlib

target = pathlib.Path(sys.argv[1])
plugin_manifest = pathlib.Path(sys.argv[2])

# Load existing settings (or empty)
try:
    settings = json.loads(target.read_text()) if target.exists() else {}
except Exception as e:
    print(f"ERROR: settings.json parse failed: {e}", file=sys.stderr)
    sys.exit(1)

manifest = json.loads(plugin_manifest.read_text())
new_hooks = manifest.get("hooks", {})

# When merging via install.sh (not via the plugin marketplace), the
# CLAUDE_PLUGIN_ROOT env var is not set in the hook shell. The plugin.json
# uses it for portability, but for install.sh-based installs we substitute
# $HOME/.claude (where install.sh has placed the hook files).
def _substitute_plugin_root(blocks):
    out = []
    for blk in blocks:
        new_inner = []
        for h in (blk.get("hooks", []) or []):
            if isinstance(h, dict) and "command" in h:
                h = dict(h)  # shallow copy
                h["command"] = h["command"].replace(
                    "${CLAUDE_PLUGIN_ROOT}", "$HOME/.claude"
                )
            new_inner.append(h)
        new_blk = dict(blk)
        new_blk["hooks"] = new_inner
        out.append(new_blk)
    return out

new_hooks = {ev: _substitute_plugin_root(blks) for ev, blks in new_hooks.items()}

# Merge hooks — preserve any non-coengineer hooks the user already has,
# replace any prior coengineer entries (idempotent re-run).
existing_hooks = settings.get("hooks", {}) or {}

def is_coengineer_hook(h):
    cmd = h.get("command", "") if isinstance(h, dict) else ""
    return "codex-coengineer/hook.py" in cmd

for event_name, blocks in new_hooks.items():
    # Each block is {"matcher": "...", "hooks": [...]} OR
    # {"hooks": [...]} (no matcher = matches all)
    cur_blocks = existing_hooks.get(event_name, []) or []
    # Strip prior coengineer entries from each existing block
    pruned = []
    for blk in cur_blocks:
        new_inner = [h for h in (blk.get("hooks", []) or []) if not is_coengineer_hook(h)]
        if new_inner:
            blk["hooks"] = new_inner
            pruned.append(blk)
    # Append the plugin-defined block(s)
    pruned.extend(blocks)
    existing_hooks[event_name] = pruned

settings["hooks"] = existing_hooks
target.write_text(json.dumps(settings, indent=2) + "\n")
print(f"merged hooks → {target}")
'

# ── Pre-validate settings.json BEFORE any file copies ────────────────
# (Codex review: validate JSON early so a malformed settings.json doesn't
# leave a partial install behind after files have already been copied.)
if [ -e "$SETTINGS_FILE" ]; then
    if ! python3 -c "import json,sys; json.loads(open(sys.argv[1]).read())" "$SETTINGS_FILE" 2>/dev/null; then
        echo "  ❌ ${SETTINGS_FILE} is not valid JSON. Fix or remove it before installing." >&2
        exit 1
    fi
    say "  ✓" "${SETTINGS_FILE} is valid JSON"
fi

# ── Backup ───────────────────────────────────────────────────────────
NEEDS_BACKUP=0
if [ -e "$HOOK_DIR" ] || [ -e "$PROMPTS_DIR" ] || [ -e "$SETTINGS_FILE" ]; then
    NEEDS_BACKUP=1
fi

if [ "$NEEDS_BACKUP" = "1" ]; then
    say "→" "Backup existing files"
    if [ "$FORCE" = "0" ] && [ "$DRY_RUN" = "0" ]; then
        printf "  Backup to %s? [Y/n] " "$BACKUP_DIR"
        read -r ans
        case "${ans:-y}" in
            n|N) echo "  Aborting."; exit 1 ;;
        esac
    fi
    run "mkdir -p \"$BACKUP_DIR\""
    [ -e "$HOOK_DIR" ]    && run "cp -R \"$HOOK_DIR\" \"$BACKUP_DIR/codex-coengineer-hooks\""
    [ -e "$PROMPTS_DIR" ] && run "cp -R \"$PROMPTS_DIR\" \"$BACKUP_DIR/codex-prompts\""
    [ -e "$SETTINGS_FILE" ] && run "cp \"$SETTINGS_FILE\" \"$BACKUP_DIR/settings.json\""
    say "  ✓" "backed up to $BACKUP_DIR"
fi

# ── Copy hook + prompts ──────────────────────────────────────────────
say "→" "Install hook + prompts"

run "mkdir -p \"$HOOK_DIR\""
run "cp \"$REPO_ROOT/hooks/codex-coengineer/hook.py\" \"$HOOK_DIR/hook.py\""
run "cp \"$REPO_ROOT/hooks/codex-coengineer/test_hook.py\" \"$HOOK_DIR/test_hook.py\""
run "cp \"$REPO_ROOT/hooks/codex-coengineer/README.md\" \"$HOOK_DIR/README.md\""
run "chmod +x \"$HOOK_DIR/hook.py\""
say "  ✓" "${HOOK_DIR}/hook.py + tests + README"

run "mkdir -p \"$PROMPTS_DIR\""
for f in spec-review.md plan-review.md phase-review.md; do
    run "cp \"$REPO_ROOT/codex-prompts/$f\" \"$PROMPTS_DIR/$f\""
done
say "  ✓" "${PROMPTS_DIR}/{spec,plan,phase}-review.md"

# ── Merge settings.json ──────────────────────────────────────────────
say "→" "Merge ${SETTINGS_FILE}"

if [ "$DRY_RUN" = "1" ]; then
    say "  [dry-run]" "would merge hooks block from .claude-plugin/plugin.json"
else
    python3 -c "$PYTHON_JSON_MERGE" "$SETTINGS_FILE" "$REPO_ROOT/.claude-plugin/plugin.json"
fi

# ── Smoke test ───────────────────────────────────────────────────────
say "→" "Smoke test"
if [ "$DRY_RUN" = "0" ]; then
    # Codex Pass-2 review: extract the actual command from settings.json
    # and run THAT (not just the direct hook path). This catches the bug
    # where settings.json contained an unsubstituted ${CLAUDE_PLUGIN_ROOT}
    # that resolved to empty, breaking the path.
    set +e
    extracted=$(python3 -c "
import json, sys
s = json.loads(open('$SETTINGS_FILE').read())
hooks = s.get('hooks', {}).get('UserPromptSubmit', [])
for blk in hooks:
    for h in blk.get('hooks', []) or []:
        cmd = h.get('command', '')
        if 'codex-coengineer/hook.py' in cmd:
            print(cmd)
            sys.exit(0)
sys.exit(1)
")
    rc=$?
    set -e
    if [ "$rc" -ne 0 ]; then
        echo "  ⚠️  could not find coengineer hook command in settings.json" >&2
    else
        # Run the extracted command (eval expands $HOME etc).
        set +e
        out=$(echo '{}' | eval "$extracted" 2>&1)
        rc=$?
        set -e
        if [ "$rc" -ne 0 ]; then
            echo "  ⚠️  hook command from settings.json failed (exit $rc): $out" >&2
            echo "  Command: $extracted" >&2
        else
            say "  ✓" "settings.json hook command → exit 0"
        fi
    fi
fi

say "✓" "Done."
echo
echo "Next steps:"
echo "  1. Restart any active Claude Code sessions (hooks load at session start)."
echo "  2. Try editing a file under src/ then running 'codex exec --skip-git-repo-check ... < /dev/null'"
echo "     to see the coengineering reminder fire/clear."
echo "  3. Read ${REPO_ROOT}/docs/CAPTURE_PATTERN.md for the codex exec invocation pattern."
echo
[ "$NEEDS_BACKUP" = "1" ] && echo "Backup of prior files: $BACKUP_DIR"
