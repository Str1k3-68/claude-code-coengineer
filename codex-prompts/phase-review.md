# Per-phase code review prompt — phase-review.md

Substitute `<PLAN_PATH>` and `<PHASE_NUM>` and `<FILES>` (comma-separated repo-relative paths) before piping to codex exec.

---

You are verifying that Phase `<PHASE_NUM>` of the implementation plan at `<PLAN_PATH>` was correctly implemented. The relevant files are: `<FILES>`.

Tests pass; that's necessary but not sufficient. Specifically check:

1. **Implementation matches plan claims** — Read the plan's Phase `<PHASE_NUM>` section and verify each numbered step is reflected in the code.

2. **Silent-green / silent-success patterns** — For each gate, threshold check, or fail-closed mechanism: could a stale, missing, or corrupt input cause the gate to return "OK" when it should raise/return an error?

3. **Threshold math** — If the implementation translates a config value (e.g., `max_staleness_days`) into an internal threshold, verify the translation arithmetic is correct. Boundary cases: gap == threshold, gap == threshold ± 1, gap == 0.

4. **Edge cases the tests don't cover** — What's the worst input that would still pass through? Future-dated data (corruption signal). Non-trading dates in trade_date columns. Empty or null values where the test only covers non-empty.

5. **Behavioral drift from plan** — Did any decisions get implemented differently than the plan specified? Even small drift compounds.

6. **Test changes** — Were any pre-existing tests modified? If so, was the modification correct, or did it accommodate a regression by loosening assertions?

7. **Cross-project consistency** — If the change touches code vendored to other repos (or vendored from), does the contract test still pin upstream==vendored equivalence?

For each finding, give:
- **File:line** citation
- **What's wrong**
- **Specific fix** (diff or precise instruction)

Reply with sections matching the questions above, then a final line:

**Verdict:** Approved-To-Phase-<NEXT> | Approved-To-Commit | Needs-Revision

If Needs-Revision, do NOT approve until the revisions are applied and a re-review is run.

Reply <400 words. Be specific; cite file:line for everything.
