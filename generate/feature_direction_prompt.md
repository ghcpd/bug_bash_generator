You are the technical lead for this project. The team is planning a round of engineering improvements for the next iteration, and you need to do a code review to identify areas worth improving.

Start by exploring the codebase (use `cat`, `find`, `grep` to read the source files), then write your improvement directions to the file `feature_plan.txt` in the current directory.

### Requirements for improvement directions

**Architecture level (at least 1):**
- Identify areas where modules do not collaborate well — e.g., duplicated logic, hardcoded dispatch, inconsistent interfaces
- Must involve interaction problems between at least 2 different modules. Do not accept improvements that only concern a single file's internal structure
- You MUST reference specific code using `filename:function_name` format. No vague descriptions without code references
- Describe what the problem is. You do not need to provide a complete solution

**Feature level (at least 2):**
- Identify areas where the current API or functionality is not flexible or complete enough
- At least 1 direction must involve changing an existing function's signature or default behavior (e.g., adding a parameter, changing return type, changing a default value)
- Prefer improvements that "seem simple to implement but have wide impact" — e.g., modifying the behavior of a utility function called from many places, or unifying logic that is duplicated with slight variations across modules
- Must reference specific functions and explain the current design's limitations
- Provide a one-sentence improvement direction. Do not write detailed signature specifications

**Do NOT suggest the following types of directions:**
- Pure documentation / comment / logging improvements
- Pure type annotation additions
- Code style unification that does not change behavior
- Pure performance optimization (e.g., adding caches, changing data structures without changing interfaces)

### Output format (follow strictly)

For each direction:

**Direction N: [one-sentence title]**
- **Problem**: Describe what is wrong or limiting in the current code. Reference specific code using `filename:function_name` format as evidence. At least 2 function references from different files per direction
- **Files involved**: List relevant existing files (at least 2)
- **Blast radius**: List which other modules/functions depend on the code being changed
- **Rough idea**: One-sentence improvement direction, no implementation details

Write the direction list to `feature_plan.txt` in the current directory. Do not output anything else to stdout.
