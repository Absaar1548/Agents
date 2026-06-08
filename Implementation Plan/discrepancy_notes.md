# BRD Agent v2.0 — Discrepancy Notes

> **Purpose:** Log all issues, surprises, and deviations discovered during implementation that were not anticipated during planning.  
> **Usage:** Add entries as you encounter them. Each entry gets a unique ID, phase reference, and resolution status.  
> **Review:** At the end of each phase, review open items. Carry forward unresolved items to the next phase or create a dedicated fix phase.

---

## Entry Format

```
### DN-XXX: [Short title]
- **Phase:** [Phase number where discovered]
- **Severity:** Low | Medium | High | Critical
- **Description:** [What happened]
- **Expected:** [What we planned]
- **Actual:** [What we found]
- **Resolution:** [How we handled it / Deferred to Phase X]
- **Status:** Open | Resolved | Deferred
```

---

### DN-001: test/ directory files temporarily deleted
- **Phase:** Phase 0
- **Severity:** Low
- **Description:** During directory creation, the tracked files in `test/` (demo_prompt.md, trace_contract.md, test_ollama.ipynb) were found as deleted in git status. Cause unknown — possibly Windows/git interaction when `tests/` was created.
- **Expected:** Old `test/` directory remains untouched while new `tests/` is created alongside it.
- **Actual:** `test/` files showed as deleted; `test/` directory itself was empty/missing.
- **Resolution:** Restored `test/` files via `git checkout HEAD -- test/`.
- **Status:** Resolved

---

## Entries

<!-- 
### DN-001: [Example title]
- **Phase:** Phase 1
- **Severity:** Medium
- **Description:** [What happened]
- **Expected:** [What we planned]
- **Actual:** [What we found]
- **Resolution:** [How we handled it]
- **Status:** Open
-->
