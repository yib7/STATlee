# Deployment Playbook — host STATlee without surprises on your bill

This is the **money-safe** guide to taking STATlee live. It is written for the
situation where you want people to *try* the app before you ever spend real
money on it.

> **TL;DR — today's stance: GitHub-only.** STATlee lives in this repo and runs
> locally. **Nothing is deployed and nothing costs money.** That is a deliberate
> choice: a clean, documented, tested repo is a strong portfolio piece on its
> own. When you decide to put it on the internet, follow Part 2 — it is built so
> the most you can spend is a number *you* choose in advance.

---

## Part 1 — Why GitHub-only is a fine place to stop (for now)

For a resume/portfolio, a recruiter values **the code and the story**, not a
running URL. This repo already gives you:

- A real, tested Flask app (135 passing tests, `ruff` clean, CI on every push).
- An architecture doc, a security audit, and this playbook.
- Sandboxed code execution, moderation, rate limiting, auth, and a billing seam.

You can demo it live on your own machine in 60 seconds (`docker-compose up`),
record a short screen capture, and link the repo. **Zero hosting cost, zero
risk.** Deploy only when you actually want public traffic.

---

## Part 2 — When you're ready to deploy (the money-safe path)

You will do these steps yourself: they involve **your** accounts, **your** API
key, and **your** card. This guide never asks you to hand those to anyone, and
the app is designed so a careless config can't quietly drain your wallet.

### 2.0 The one rule that makes this safe

**Your only real spend is the Gemini API.** Hosting can be free; the API is what
costs money per request. So the entire strategy is: *cap the API spend at the
source, then cap how fast anyone can reach it.*

### 2.1 Pre-deploy money-safety checklist (do ALL of these)

- [ ] **Set a hard spend limit on the Gemini key itself.** In
      [Google AI Studio / Cloud billing](https://aistudio.google.com/apikey),
      create a key tied to a project with a **budget cap and alerts**. This is
      your ultimate backstop — even if everything else fails, Google stops
      billing past your cap. *Do this first.*
- [ ] **Keep `BILLING_ENABLED=false`** while trialing (free for users, no
      payment processor needed). Users still can't run wild because of the next
      two items.
- [ ] **Set `MONTHLY_PRIORITY_CALL_CEILING` to a low number** (e.g. `200`). This
      caps the expensive priority-tier calls per month on your key. The app
      **warns at startup** if billing is on with no ceiling.
- [ ] **Leave rate limits on** (`RATE_LIMIT_ENABLED=true`) and back them with a
      **shared store** (`RATELIMIT_STORAGE_URI=redis://...`) *or* pin
      `WEB_CONCURRENCY=1`. Otherwise each worker counts separately and the cap
      is weaker.
- [ ] **Set `WRANGLE_ROLE=lite` and keep `CONVERSE_ROLE=flash`** so the chatty,
      high-volume features use the cheapest models. (These are the defaults.)
- [ ] **Set a short `FILE_TTL_SECONDS`** (default 2h) so anonymous uploads are
      cleaned up and storage doesn't grow.
- [ ] **Never commit `.env`.** It's gitignored; keep it that way. Set secrets in
      the host's dashboard, not in the repo.
- [ ] **Generate a real `FLASK_SECRET_KEY`** (`python -c "import secrets;
      print(secrets.token_hex(32))"`).

### 2.2 Pick a free host

Any of these has a **$0 free tier** big enough for a trial. None requires a card
to start (verify current terms when you sign up — free tiers change):

| Host | Free tier shape | Notes |
|---|---|---|
| **Render** | Free web service (sleeps when idle) | Already wired: `Dockerfile`, `docker-compose.yml`, ProxyFix defaults to 1 hop in prod. Simplest path. |
| **Fly.io** | Small free allowance | Good if you want it always-on; uses the same Docker image. |
| **Railway** | Trial credit | Easy Postgres add-on. |

This repo ships a `Dockerfile` and `docker-compose.yml`, so any Docker host works.

### 2.3 Deploy (Render example)

1. Push this branch to GitHub (you already have the remote).
2. In Render: **New → Web Service → connect the repo**. It auto-detects the
   `Dockerfile`.
3. Add environment variables from your checklist above (`GEMINI_API_KEY`,
   `FLASK_SECRET_KEY`, `APP_ENV=production`, `MONTHLY_PRIORITY_CALL_CEILING=200`,
   etc.). **Never paste these into the repo.**
4. Add a free **Redis** instance (or set `WEB_CONCURRENCY=1`) and point
   `RATELIMIT_STORAGE_URI` at it.
5. Deploy. Open the URL, upload a small CSV, confirm it works.

> The current live URL in the README (`codecaster-th8m.onrender.com`) is from a
> previous deploy and predates the STATlee rename — renaming the Render service
> and updating that link is a deferred task (see `.autopilot/BACKLOG.md`).

### 2.4 After it's live — watch the meter

- Check the **Gemini billing dashboard** weekly. Your budget alert should email
  you long before anything concerning.
- Watch the app logs for the rate-limit and ceiling warnings.
- If trial usage is higher than expected, **lower the ceiling** — it takes
  effect on the next restart.

### 2.5 Turning on real payments (a separate, deliberate step)

Charging money means a payment processor (Stripe) and **new secrets** — that is
explicitly a human decision, not something to automate. The app is ready for it
(`billing.py` + `User.credits`); see [PRICING.md](PRICING.md) for the model and
the exact wiring steps when you choose to do it.

---

## What NEVER to do (the footguns)

- ❌ Deploy with `BILLING_ENABLED=true` and `MONTHLY_PRIORITY_CALL_CEILING=0` —
  that's an uncapped bill. (The app warns, but don't rely on the warning.)
- ❌ Expose the app with `RATE_LIMIT_ENABLED=false`.
- ❌ Run multiple workers with `memory://` rate-limit storage (caps don't hold).
- ❌ Commit `.env` or paste your `GEMINI_API_KEY` anywhere public.
- ❌ Skip the Gemini-side budget cap. It is the one control that *cannot* be
  bypassed by an app bug.
