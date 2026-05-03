# Spec sheet review prompt — spec-review.md

Substitute `<SPEC_PATH>` with the actual spec file path before piping to codex exec.

---

You are reviewing a spec sheet at `<SPEC_PATH>` for a non-trivial feature, refactor, or production-affecting change. The spec is the input to a forthcoming implementation plan; catch ambiguity now, not in code.

Read the spec carefully and answer:

1. **Self-consistency** — Are there contradictions between sections? (e.g., section X says A but section Y implies B)
2. **Riskiest assumption** — Which assumption, if wrong, would invalidate the largest portion of the work? What evidence backs it?
3. **Missing edge cases** — Specifically check for: weekends/holidays/DST, partial failures, race conditions, cross-system clock skew, schema migration on existing data, idempotency of new-data writes, fail-closed vs fail-open semantics, units (calendar days vs trading days, fractions vs percents, UTC vs local).
4. **Wrong-but-satisfies-spec implementations** — Sketch one. If the spec admits an obviously broken implementation, the spec is under-constrained.
5. **Acceptance criteria coverage** — Are the listed acceptance criteria sufficient to demonstrate the feature works? Any silent-success modes the criteria don't catch?
6. **Cross-project / cross-repo concerns** — Does the spec touch shared interfaces, contracts, vendored code? Coordination plan adequate?
7. **Out-of-scope clarity** — Are explicit non-goals listed? Anything ambiguous about what's NOT included?

Reply with sections matching the questions above, then a final line:

**Verdict:** Approved-To-Plan | Needs-Revision

If Needs-Revision, give specific edit diffs (`- old line\n+ new line`) for the spec, not narrative suggestions.

Reply <500 words. Treat me as a peer, not a customer; push back hard if something is wrong.
