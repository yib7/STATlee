# Autopilot — Autonomy Contract

This run is on **autopilot**. The orchestrator and every subagent it dispatches follow this
contract. The orchestrator MUST restate this contract in each subagent's brief — do not assume it
is loaded from anywhere.

## Hard stop — ask the human, then wait

Stop and ask ONLY for:

1. **Secrets / credentials** — needing, creating, printing, committing, or rotating an API key,
   token, password, private key, or `.env` secret.
2. **Real money** — anything that spends actual money or paid API / credits, places an order, or
   incurs billable cloud cost.

That is the whole stop list. Nothing else pauses the run.

> This cycle is money-sensitive by the user's explicit request ("100% safe with money"). The user
> chose GitHub-only + a deploy *playbook*: **deploy nothing, create no accounts, wire no payment
> processor, spend $0.** Treat any drift toward real deployment or billable cost as a hard stop.

## Everything else: decide and keep going

For all other decisions — including reversible-but-scary ones — pick the sensible default,
proceed, and log one line to `DECISIONS.md`. This explicitly includes:

- Refactors, file moves/deletes, schema changes, dependency installs.
- Destructive-but-local ops: dropping a local/test DB, rewriting **un-pushed** git history,
  resetting the working tree on this branch.
- Touching prod-shaped code paths **inside the isolated branch** (you are writing, not deploying).

## The safety net (why the short stop list is OK)

Because the stop list is intentionally minimal, **isolation is the safety net, not the stop
button.** This run happens on a dedicated worktree/branch — never `main`, never a live deployment.
The ONE human gate is the final merge.

Do **not**, as part of the loop: merge to `main`, deploy, `push --force` to a shared branch, or
run against a live/production system. Those are the human's call at the very end.

## Logging

- Every non-trivial assumption or reversible decision → one line under **Resolved** in
  `DECISIONS.md`: `[date] <phase> — <decision> — <why> — how to undo: <...>`.
- A rare non-blocking question for the human → **Open** in `DECISIONS.md` (answered in one pass
  when they return); keep running the sensible default meanwhile.
- New unrelated ideas → `BACKLOG.md` Inbox (brainstormed as the *next* cycle), not this run.
