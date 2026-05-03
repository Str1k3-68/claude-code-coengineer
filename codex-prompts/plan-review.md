# Implementation plan review prompt — plan-review.md

Substitute `<PLAN_PATH>` with the plan file path before piping to codex exec.

---

You are reviewing an implementation plan at `<PLAN_PATH>` produced by the `superpowers:writing-plans` skill (or equivalent). The plan typically has phases with concrete steps, file paths, expected effort, and acceptance criteria. The next step is implementing Phase 1.

Read the plan and answer:

1. **API surface review** — For any new public functions, classes, or modules: signatures, return types, defaults, parameter names. Anything that future maintainers will trip over? Naming inconsistent with existing project conventions?

2. **Phase ordering** — Can phase N proceed safely with phase N-1's incomplete state? Are dependencies between phases explicit? Any "this phase depends on a side-effect of an earlier phase that isn't documented" gotchas?

3. **Test coverage gaps** — What edge cases must Phase 1's tests pin? Real-calendar dates / DST transitions / holidays where applicable. Fail-toward-red invariants. Boundary conditions (==, < vs ≤). Contract tests for vendored code.

4. **Threshold / constant values** — Are numeric thresholds (cutoffs, retry counts, timeouts, max-staleness) justified? Empirical or arbitrary?

5. **Cross-project propagation order** — If the plan vendors helper code or modifies shared interfaces, is the propagation order safe? (Upstream first, then dependents, with contract tests.)

6. **Acceptance criteria** — Could a phase pass its acceptance criteria while the feature is broken? List specific silent-success modes to add to the criteria.

7. **Out-of-scope** — Does the plan explicitly list non-goals? Anything in the plan that should be deferred to a follow-up?

8. **Coengineering integration** — Does the plan call for codex review at each phase boundary, or only at the end? (Should be at each.)

Reply with sections matching the questions above, then a final line:

**Verdict:** Approved-To-Phase-1 | Needs-Revision

If Needs-Revision, give specific edit diffs (`- old line\n+ new line`) for the plan, not narrative suggestions. Use file:line citations from the plan when referring to specific text.

Reply <600 words. Be terse and direct. Push back hard on anything that smells.
