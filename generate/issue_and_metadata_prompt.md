A colleague introduced a regression bug while developing a new feature. You need to complete the following tasks:

### Task 1: Write an issue report

Write a bug report as an **end user**. You do not know what code was changed — you only know that something broke after upgrading.

Writing guidelines:
- Describe the **symptoms** you observed — what feature stopped working, what error messages you saw
- Use **vague language** — "the output looks wrong", "the formatting changed", do not describe character-level differences
- **Guess wrong** about the cause — suspect dependency versions, environment issues, encoding differences, etc.
- Focus on **downstream impact** — "my docs won't render anymore", "CI is failing"
- Do **NOT** mention any function names, file names, line numbers, variable names, or class names
- Do **NOT** do expected vs actual character-by-character comparisons
- Do **NOT** include code snippets that call internal functions
- Write like a real user — rambling, including irrelevant environment details, unable to pinpoint the real cause

### Task 2: Fill in metadata

Based on the diff and test failure output you see below, fill in the following JSON fields.

Output format (follow strictly):

First output the issue report text, wrapped in `ISSUE_START` and `ISSUE_END` markers.

Then output a metadata JSON block, wrapped in `METADATA_START` and `METADATA_END` markers:

```
ISSUE_START
[your issue report text]
ISSUE_END

METADATA_START
{
  "feature_keyword": "A 2-4 word slug describing the added feature, joined by underscores. E.g.: bytearray_support",
  "category": "Pick one: Logic & Algorithm | Data Handling & Transformation | API & Interface Contract | Error Handling & Edge Cases | Configuration & Environment | Type & Validation",
  "sub_type": "Specific bug subtype, e.g.: rounding_behavior_change, type_change, string_encoding_format",
  "difficulty": "L1-L4 based on localization difficulty: L1=error message points directly L2=need to read code L3=need to understand module interaction L4=need to understand implicit contracts",
  "mutation_file": "Relative path of the main file where the bug was introduced",
  "mutation_description": "One sentence describing the feature that was added",
  "repo_description": "One sentence describing what this project does",
  "feature_description": "One sentence describing the feature that was added",
  "bug_description": "One sentence describing the bug that was introduced"
}
METADATA_END
```

Output only the above content, nothing else.
