# CLAUDE.md

- The single source of truth for this project is RFQ-SPEC.md. Read it before starting any work.
- RFQ-SPEC.en.md is its English translation (the Chinese file wins). Whenever RFQ-SPEC.md changes, update RFQ-SPEC.en.md in the same commit; `tests/test_docs.py` checks that their structure matches.
- Strictly follow the working rules in Section 0 of RFQ-SPEC.md.
- All code comments and docstrings must be in English.
- Use fake LLM mode for tests. Ask before running anything that calls a real LLM API.
- If a business rule is unclear, ask instead of making assumptions.

## Phase workflow

Each Phase runs in its own fresh session; a separate review session does acceptance. A new session knows only what is in the repo, so every decision lives in a file:

- Business rules -> RFQ-SPEC.md (edit the relevant section once the user decides), mirrored in RFQ-SPEC.en.md.
- Domain terms -> GLOSSARY.md, mirrored in GLOSSARY.en.md in the same commit (`tests/test_docs.py` checks every term has an English entry).
- Per-Phase design decisions -> `docs/plans/phase-N.md` (decision table, as in phase-2.md).

### Implementation session

1. Read RFQ-SPEC.md, `docs/plans/`, and any handoff doc the user points to. Create branch `phase-N-<slug>` from `main`.
2. Run `/grilling` on the Phase plan. Done when every open question is answered and written to the files above.
3. Build with `/tdd`. Use `/diagnosing-bugs` for any bug that resists a first fix.
4. Stop and report: what was done, test results, real-LLM smoke result (if the Phase calls for one), and the plan for the next Phase.
5. After the review session passes and the user confirms: write the merge description with `/pr`, commit on the branch, and merge into `main` with `--no-ff`.

### Review session

Read only; leave code changes to the implementation session. Run `/code-review <last commit of the previous Phase>` and check the RFQ-SPEC.md section 17 acceptance criteria for the Phase. Report pass/fail per criterion, plus issues ranked by severity.
