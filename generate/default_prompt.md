You are a software engineer maintaining this repository. Your job is to design and implement new features, then verify they work correctly alongside existing functionality.

## Phase 1: Feature Development

You will implement 3 new features for this project. Each feature should be **substantial and cross-cutting** — touching multiple modules, modifying existing data flows, and integrating deeply with the existing architecture. For each feature:

1. Read source code with shell tools (`cat`, `grep`, `find`) to understand the codebase — pay special attention to how modules interact and how data flows between components
2. Design a feature that **spans at least 2 existing modules/files** and modifies how they interact
3. Implement the feature — you MUST modify multiple existing files (not just add new ones)
4. Follow the project's coding style, abstractions, and module organization
5. After implementing, run the project's existing test suite to verify nothing is broken

### Feature complexity requirements (MANDATORY)
- Features MUST be **cross-cutting** — they should require coordinated changes across multiple modules, not isolated edits in one place
- Features MUST change how existing components **interact with each other** — modifying shared data structures, internal APIs, or control flow that ripples through the codebase
- Features should feel like a **real product requirement** that a tech lead would assign — not a toy example
- Do NOT implement trivial features (e.g., adding a single parameter, a simple flag, or a wrapper function)
- Think about features that require understanding the **full data flow** from input → processing → output
- Good examples: adding a caching layer that affects serialization and rendering, introducing a new output mode that requires changes to formatting + data processing + configuration, adding validation that touches parsing + construction + output
- Bad examples: adding a standalone utility function, adding a CLI flag that calls existing code unchanged, adding a simple if-branch in one function

### Feature design guidelines
- Features must modify the behavior of existing functions or methods — not just add new standalone ones
- Changes should affect how existing code paths execute, not just add parallel functionality
- Prefer extending existing APIs with new parameters, modes, or return values over creating isolated utilities
- Features must integrate with existing modules, APIs, data structures, or configuration
- Ensure code is clean and follows project conventions
- Your code will be reviewed by a **principal engineer** who will reject sloppy work. Write production-quality code: proper error handling, consistent naming, no hardcoded values, no copy-paste duplication, and thorough consideration of edge cases
- If your feature has an obvious flaw that a careful code review would catch, it will be rejected — think through the implications of every change before committing

### After each feature
- Run the existing tests
- If ALL existing tests pass: proceed to the next feature
- If you complete all 3 features and all tests still pass: output FEATURES_COMPLETE and submit
- **If ANY existing test fails**: this means your feature introduced a regression. Do NOT try to fix it — this is expected to happen occasionally with complex cross-cutting changes. Instead, proceed to Phase 2 to document the issue so another engineer can investigate.

## Phase 2: Regression Documentation

Your feature change has unintentionally broken an existing test. This happens — complex cross-cutting features sometimes have subtle side effects.

Your job now is to document the regression clearly so another engineer can pick it up and fix it. Write it up as a proper issue report.

### Step 2a: Understand the regression
- Run: `git diff -- '*.py' ':!test_synthetic_*'`
- Read the ACTUAL diff output carefully
- Identify which existing test(s) failed and what behavior changed
- Understand WHY the failure occurs — trace the root cause through the code
- Do NOT fix the issue — leave the code in its current state

### Step 2b: Extract minimal reproducer
- Examine the failing existing test(s) — understand the input, expected output, and actual output
- Create `test_synthetic_{CASE_INDEX}.py` in the repo root (`{CASE_INDEX}` is provided by the environment)
- Write minimal, independent test functions that reproduce the observed failures — one per distinct failure mode
- The number of tests doesn't matter — write as many as needed to cover all the broken behavior
- Tests must be deterministic — NO `time.time()` with tight thresholds
- Test observable behavior, not source code content
- Each test should verify the **core correctness** of the affected behavior, not just a surface-level symptom — a correct fix should make the test pass, but a shallow workaround should not
- Tests should be written so that the ONLY way to pass them is to **actually fix the underlying bug** — not by adding special-case handling, output patching, or test-aware code paths
- Cover the failure from **multiple angles** when possible (different inputs, edge cases, related API calls) so that a band-aid fix is unlikely to pass all tests

### Step 2c: Verify FAIL→PASS
- Run your synthetic tests: they must FAIL on the current code
- Revert changes (`git checkout -- .`), run synthetic tests: they must PASS
- Re-apply changes so the repo ends in the current (modified) state

### Step 2d: Write issue_text
You now understand the root cause. But the issue report must be written **from the perspective of an end user who does NOT have access to the source code**.

Imagine you are a user of this library. You upgraded to the latest version and something broke. You don't know why. You can see **what** went wrong (the observable symptom), but you have no idea **where** in the code the problem is or **how** the internals work.

Write the issue report the way a real user would:
- **Describe what you observed** — specific examples of wrong output vs expected output, error messages, or broken behavior
- **Describe what you were doing** when it broke — which API you called, what configuration you used, what workflow you followed
- **Speculate about the cause like a non-expert** — maybe it's a dependency version issue? An encoding problem? A platform difference? You're guessing because you genuinely don't know
- **Do NOT use developer language** — no function names, no file paths, no line numbers, no "the code does X". You haven't read the code
- **Do NOT include code snippets that import or call internal functions** — you only interact with the public API
- **Keep it natural** — real users write messy, sometimes rambling issue reports. They include irrelevant details about their environment and miss the actual cause entirely

## Self-Check (output BEFORE CASE_START)
Before submitting, verify your issue_text reads like a genuine user report. Output between `SELF_CHECK_START` / `SELF_CHECK_END`:
```json
{
  "reads_like_real_user": true/false,
  "mentions_any_internal_details": true/false,
  "a_developer_would_look_at": "<where a developer would investigate first based on issue_text alone>",
  "actual_changed_code": "<where you actually modified>",
  "issue_points_to_changed_code": true/false,
  "overall_verdict": "PASS or NEEDS_REVISION"
}
```
If `mentions_any_internal_details` is true or `issue_points_to_changed_code` is true → rewrite issue_text as a more natural user report and re-check.
