# Web frontend (optional, separate from the Claude Code workflow)

A small Flask app + single HTML page for evaluating job fit and drafting
cover letters/CV bullets outside Claude Code. Two modes:

- **`local`** — single user, no login, no credit limits. Run on your own machine.
- **`hosted`** (default) — email/password accounts, a credit balance per user,
  Stripe Checkout to buy more credits. Needed if you put this on a public URL
  for more than yourself.

## Local mode

```bash
cd web
pip install -r requirements.txt
export APP_MODE=local
export ANTHROPIC_API_KEY=sk-ant-...
python app.py
```

Open http://127.0.0.1:5050. No signup — paste your profile, add applications,
evaluate/tailor. Local mode skips the landing page and goes straight to the
dashboard (`web/static/index.html`); it's a single-user tool, so there's
nothing to market.

A dark/light toggle lives in the dashboard's sidebar; it follows the OS
preference by default and remembers your choice in the browser
(`localStorage`, so it won't follow you across devices).

## Hosted mode

In hosted mode, `/` serves a marketing landing page
(`web/static/landing.html`) and the dashboard moves to `/app`. Both honor the
same dark/light toggle and preference.

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY, SECRET_KEY at minimum
export $(grep -v '^#' .env | xargs)
python app.py
```

Without `STRIPE_SECRET_KEY`/`STRIPE_WEBHOOK_SECRET` set, billing is simply
disabled — signup still grants `FREE_CREDITS` and the app runs free-only.

To enable paid top-ups:
1. Create a [Stripe](https://stripe.com) account, grab the secret key.
2. Add a webhook endpoint at `<your-domain>/api/billing/webhook` listening for
   `checkout.session.completed`, grab its signing secret.
3. Set both in `.env`. Credit packs are defined in `billing.py` (`PACKS`).

In production run behind `gunicorn` and a reverse proxy with TLS. Set
`TRUSTED_PROXY_HOPS=1` (or however many proxies sit in front of the app) so
rate limiting and the per-IP signup cap see the real client IP from
`X-Forwarded-For` instead of the proxy's own address:

```bash
TRUSTED_PROXY_HOPS=1 gunicorn -w 1 -b 127.0.0.1:5050 app:app
```

Keep `-w 1` (one worker): the rate limiter and signup cap use in-process
memory, so running multiple workers lets each one track its own counters
instead of sharing them, weakening both limits. For more than one worker,
point `Limiter(..., storage_uri=...)` in `app.py` at Redis first.

`DATABASE_PATH` is a SQLite file — fine for one small deployment; swap in
Postgres if you outgrow it.

## What it does and doesn't do

- Calls `ANTHROPIC_API_KEY` server-side (your key — hosted users spend your
  balance, which is what credits meter).
- Does **not** run `/apply`, `/rank`, etc. from the main Claude Code workflow.
  It's a simpler, two-action subset (evaluate fit, tailor CV+letter) meant for
  a browser instead of Claude Code.
- Stores applications and profile text in SQLite, not in `documents/` or
  `job_search_tracker.csv` — the two aren't currently linked.
