# Pricing — model & rationale

> **Status: illustrative.** This describes how STATlee *would* be priced and how
> the codebase already supports it. **Billing is OFF** (`BILLING_ENABLED=false`)
> and no payment processor is wired. It exists so the monetization story is clear
> for a portfolio/resume and so turning it on later is a small, deliberate step —
> not a rewrite.

## The cost basis

STATlee's only per-use cost is the **Gemini API**. Different features use
different model tiers, cheapest-first:

- **Conversational data cleaning** (`WRANGLE_ROLE`) and moderation → `lite`
  (cheapest). High volume, short prompts.
- **Codebook, suggestions, converse** → `flash`.
- **Priority generation** (the "make this one count" toggle) → `pro` (most
  capable, most expensive).

Because expensive calls are isolated to the priority tier, pricing can give
everyone generous *standard* use and meter only the premium path.

## Illustrative tiers

| Tier | Price | What you get | How it maps to the code |
|---|---|---|---|
| **Free** | $0 | Standard analysis, conversational cleaning, undo/redo, export. A small monthly allotment of **priority** generations. | `User.plan='free'`, `User.credits` seeds the priority allotment; standard calls aren't debited. |
| **Student** | ~$5/mo | Everything in Free + a larger priority allotment + saved history. | Same seam, higher monthly `credits`; accounts already persist datasets/history. |
| **Pro** | ~$15/mo | Generous priority use, larger uploads, priority support. | Higher `credits` (or unmetered priority), tunable upload/row caps already in `config.py`. |

*(Numbers are placeholders to illustrate the structure — set real ones against
actual Gemini token costs before charging anyone.)*

## What's already built (the seam)

The monetization chokepoint is **one function**, `billing.check_and_debit`
(`statlee/billing.py`), called on the request path:

- **No-op until enabled.** With `BILLING_ENABLED=false` it always authorizes —
  so the trial is free and the wiring is dormant, not absent.
- **Operator protection.** A global `MONTHLY_PRIORITY_CALL_CEILING` caps priority
  spend on the server's own key regardless of who calls (see
  [DEPLOYMENT_PLAYBOOK.md](DEPLOYMENT_PLAYBOOK.md)).
- **Per-account credits.** When enabled, a logged-in free-plan user's priority
  request debits `User.credits`; out of credits → a clear "upgrade or wait"
  message. Nothing outside `billing.py` reads or writes `credits`.

`User.plan` and `User.credits` already exist in `statlee/models.py`. The priority
toggle already routes through the role-based LLM service.

## Turning it on (later, deliberately — touches money + secrets)

This is intentionally a human step, not part of any automated run:

1. Decide real prices against measured Gemini costs.
2. Add **Stripe** (Checkout + a webhook). The webhook credits `User.credits` /
   sets `User.plan` on successful payment.
3. Store the Stripe keys as **host secrets** (never in the repo).
4. Set `BILLING_ENABLED=true` **and** a sane `MONTHLY_PRIORITY_CALL_CEILING`.
5. Add a billing/upgrade page in the UI that links to Stripe Checkout.

Until then, the honest, zero-risk position is: **free trial, billing off, spend
capped at the Gemini key.**
