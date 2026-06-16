---
description: Run after every code change in this project. Verifies tests shipped with the change, classifies it as a bug fix or new feature, proposes a targeted CLAUDE.md update, and writes it after confirmation. Never commit code without running this. Invoke proactively — do not wait to be asked.
---

# Update CLAUDE.md

Run this after every code change. Follow these steps in order — do not skip any.

## Step 1: Verify tests shipped

Run `git diff HEAD --stat` (or `git diff HEAD~1 --stat` if already committed) to see changed files.

If files under `portfolio/` changed but nothing under `tests/` changed, **stop here**:

> Tests are missing. Every code change must ship with tests — bug fixes especially, since they prove the regression is caught. Add tests first, then run this command again.

Do not proceed to the next step until tests are present.

## Step 2: Read the diff

Run `git diff HEAD` (or `git diff HEAD~1` if committed). Understand what specifically changed and why — not just what lines differ, but what behavior or assumption the change corrects or introduces.

## Step 3: Classify the change

**Bug fix / unexpected behavior catch**: Something was wrong and got corrected. The code was making a false assumption, missing a valid input case, using a wrong name/value, or silently discarding data. The fix reveals that the system's actual behavior differed from what someone reading the code would expect.

**New feature**: A capability that didn't previously exist was added — a new input format is handled, a new output is produced, a new configuration is respected, etc.

## Step 4: Decide what belongs in CLAUDE.md

Add to CLAUDE.md only if a future developer — or a new Claude session starting cold — would be **surprised** by this behavior or make the same mistake. Skip it if the code makes the behavior obvious on inspection.

Strong candidates:
- An assumption that turned out to be false (e.g. "Merrill exports sometimes ship with non-standard filenames")
- A behavior that looks wrong but is correct (e.g. parentheses = negative amounts)
- A name, formula, or API value that's easy to get wrong (e.g. `GOOGLEFINANCE` not `GOOGFINANCE`)
- A new rule that changes how inputs are classified or routed
- A constraint that's invisible from the code but would cause silent failures if violated

Skip:
- Changes whose behavior is obvious from reading the updated code
- Pure refactors with no behavioral change
- Implementation details that don't affect callers or users

## Step 5: Route to the right CLAUDE.md section

| Change type | Target section |
|---|---|
| Non-obvious behavior or bug catch | `## Non-Obvious Behaviors` — add a numbered item |
| New filename or CSV pattern | `## Merrill Edge CSV Schemas` — update the relevant schema block |
| New architectural decision or constraint | `## Key Architectural Decisions` |
| New feature with no clear existing home | Most relevant existing section; add a note explaining it |

## Step 6: Propose before writing

Show the user exactly what you plan to add before touching the file:

```
Proposed CLAUDE.md addition
  Section: ## Non-Obvious Behaviors (item N)

  N. Short title: One or two sentences describing the behavior clearly enough
     that someone reading it cold would immediately understand the constraint
     and why it exists.
```

Wait for the user to confirm or revise. Then write it.

## Step 7: Write — surgical edit only

Make the smallest possible edit: add the proposed text to the right location. Do not reformat surrounding content, renumber existing items preemptively, or restructure sections.

## Step 8: Show the diff

Run `git diff CLAUDE.md` and display it so the user can see exactly what changed before the next commit.
