# VocalPay

A natural-language payment assistant: tell it "Pay 12 to John for dinner" (or speak it), and it parses the
intent, runs it through a risk/policy engine, requires the right confirmation (typed phrase or PIN), and
executes the payment through Stripe's test API — with every step written to a tamper-evident, hash-chained
audit log. It also includes a LangChain + Gemini support chatbot that remembers your conversation and can
answer questions about your contacts, transactions, and account — without ever being able to move money itself.

**Live demo:** https://vocalpay.onrender.com

## What it does

- **Understands flexible phrasing**, not just one rigid template — `"Pay 12 to John for dinner"`,
  `"Pay 30 AUD on Alice account"`, `"Send John 12 aud"`, even self-corrections like `"Pay 20, no 30 to Smith"`.
- **Pays by name or by PayID number directly** — `"Pay 12 to 0412 345 678"` resolves against your saved
  contacts or a 200-entry mock PayID directory, auto-provisioning a real (test-mode) Stripe receiver account
  on the spot if it's a valid, unsaved PayID. Saving a contact is always optional, never a precondition to pay.
- **Real receiver-side proof**, not just "card charged" — payments to a contact use a Stripe Connect
  destination charge, and the backend confirms the money actually landed in *their* account by reading their
  live Stripe balance back, not just trusting its own database.
- **Risk-based step-up confirmation** — a typed `CONFIRM` phrase for routine payments, a 4-digit PIN for new
  payees or amounts over the cap, with attempt limits and TTL expiry.
- **Tamper-evident audit log** — every action is a hash-chained event; `GET /audit/{session_id}/verify`
  recomputes the whole chain and reports exactly where it's broken if tampered with.
- **A support chatbot with real memory** — ask "what was my last payment?" or "who are my contacts?" and it
  answers using live tool calls against your actual data, remembering the conversation across visits. It can't
  send money; real payments always go through the deterministic flow above.
- **Voice input** via the browser's native Speech Recognition API.

## Tech stack

FastAPI + raw `sqlite3`/Turso (libSQL) · Stripe SDK (test mode, Connect) · LangChain + Google Gemini ·
vanilla HTML/CSS/JS frontend (Tailwind via CDN, no build step) · pytest

## Running it locally

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt

cp .env.example .env            # then fill in STRIPE_SECRET_KEY at minimum
uvicorn main:app --reload
```

Open **http://127.0.0.1:8000** — FastAPI serves both the API and the chat frontend from the same origin.

Only `STRIPE_SECRET_KEY` (a Stripe **test-mode** key from https://dashboard.stripe.com/test/apikeys) is
required to run payments. `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` and `GEMINI_API_KEY` are optional — without
them, the app falls back to a local SQLite file and the support chatbot returns a clear "not configured"
error instead of the rest of the app breaking. See [.env.example](.env.example) for all four.

The demo account (`demo-user`, PIN `1234`) is seeded automatically on first run — no signup/login exists.

## Running the tests

```bash
pytest
```

46 integration tests covering the payment pipeline, PIN lockout/expiry, audit tamper-detection, PayID
resolution, duplicate-contact-name handling, and the support chatbot (LLM calls mocked, no API key needed).

## Deploying

Configured for [Render](https://render.com) via [render.yaml](render.yaml) — a free web service that builds
straight from this repo. See [PROJECT_STATUS.md](PROJECT_STATUS.md) §2b/§2c for how production persistence
(Turso) and the chatbot's API key are wired up as environment variables.

## Docs

[PROJECT_STATUS.md](PROJECT_STATUS.md) is the detailed build log — data model, every endpoint, every design
decision and why, known gaps, and a full file map. Read this README for the pitch; read that for the internals.

## Known limitations

Single hardcoded demo user (no real auth), AUD-only, CORS wide open (fine for this demo, not for a real
deployment), and a few demo-mode Stripe conveniences (`seed_test_card`, `create_test_connected_account`) that
intentionally bypass real card entry / KYC onboarding — see PROJECT_STATUS.md §9 for the complete list.
