# Autonomy Contract — final.md Ship Run

**Hard-stop ONLY for:**

1. **Secrets / credentials** — needing, creating, printing, committing, or rotating a key, token, password, or `.env` secret.
2. **Spending real money / paid API credits** — placing an order, incurring billable cost.

**Everything else** whose undo is a git operation on this worktree — reversible-but-scary and destructive-local ops (lint fixes, test rewrites, commit history rewrite, file deletes, directory reorganization) — gets a sensible default, proceeds, and logs one line to `DECISIONS.md`.

**Out of loop** (human gate only):
- Merging to `main` or pushing to origin.
- Opening PRs or issues.
- Publishing releases publicly.
- Deploying to production.

**User consent given:** final.md states "This file is my written consent to make any change needed, **except** changes that are destructive **and** irreversible, or that cost money."

**Skip rule:** If a phase fails its checkpoint after ~2–3 genuine attempts, log the failure to `PLAN.md` and skip to independent phases; a blocker doesn't freeze the whole run if other phases don't depend on it.

**Progress watchdog:** If ~3 consecutive dispatches finish with zero new checkboxes ticked, stop and notify the user.

**Reference the full contract here, not separately,** for subagent briefs.
