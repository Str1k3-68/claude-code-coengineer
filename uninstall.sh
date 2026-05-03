#!/usr/bin/env bash
#
# Uninstall claude-code-coengineer from ~/.claude/.
#
# Removes:
#   - ~/.claude/hooks/codex-coengineer/
#   - ~/.claude/codex-prompts/
#   - The hooks block entries pointing at codex-coengineer/hook.py from
#     ~/.claude/settings.json (other hooks are preserved)
#
# Does NOT remove:
#   - State files at ~/.claude/hooks/state/codex-coengineer/ (kept for
#     audit/forensics; rm them yourself if you don't need them)
#   - Backup directories at ~/.claude/.coengineer-backup-*
#
# Usage:
#   ./uninstall.sh
#   ./uninstall.sh --dry-run
#
set -euo pipefail

CLAUDE_DIR="${HOME}/.claude"
HOOK_DIR="${CLAUDE_DIR}/hooks/codex-coengineer"
PROMPTS_DIR="${CLAUDE_DIR}/codex-prompts"
SETTINGS_FILE="${CLAUDE_DIR}/settings.json"

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --help|-h) sed -n '3,18p' "$0" | sed 's/^# //;s/^#$//'; exit 0 ;;
    esac
done

run() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "  [dry-run] $*"
    else
        eval "$@"
    fi
}

echo "→ Strip coengineer hooks from $SETTINGS_FILE"
PYTHON_JSON_STRIP='
import json, sys, pathlib

target = pathlib.Path(sys.argv[1])
if not target.exists():
    print("  no settings.json — nothing to do")
    sys.exit(0)

settings = json.loads(target.read_text())
hooks = settings.get("hooks", {}) or {}

def is_coengineer_hook(h):
    return isinstance(h, dict) and "codex-coengineer/hook.py" in h.get("command", "")

stripped = 0
for event_name, blocks in list(hooks.items()):
    new_blocks = []
    for blk in blocks or []:
        inner = [h for h in (blk.get("hooks", []) or []) if not is_coengineer_hook(h)]
        if inner:
            blk["hooks"] = inner
            new_blocks.append(blk)
        else:
            stripped += 1
    if new_blocks:
        hooks[event_name] = new_blocks
    else:
        del hooks[event_name]

settings["hooks"] = hooks
target.write_text(json.dumps(settings, indent=2) + "\n")
print(f"  stripped {stripped} coengineer hook block(s)")
'
if [ "$DRY_RUN" = "0" ]; then
    python3 -c "$PYTHON_JSON_STRIP" "$SETTINGS_FILE"
else
    echo "  [dry-run] would run python merge-strip on $SETTINGS_FILE"
fi

echo "→ Remove $HOOK_DIR"
[ -d "$HOOK_DIR" ] && run "rm -rf \"$HOOK_DIR\"" || echo "  (already gone)"

echo "→ Remove $PROMPTS_DIR"
[ -d "$PROMPTS_DIR" ] && run "rm -rf \"$PROMPTS_DIR\"" || echo "  (already gone)"

echo "✓ Uninstalled."
echo
echo "Note: state files at ~/.claude/hooks/state/codex-coengineer/ were kept."
echo "      Remove them with: rm -rf ~/.claude/hooks/state/codex-coengineer/"
