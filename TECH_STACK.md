# VocalPay — Technologies & Concepts

A reference for everything used in this project and why. For setup instructions see
[README.md](README.md); for the mobile build specifically see [mobile/README.md](mobile/README.md); for the
phase-by-phase build history see [PROJECT_STATUS.md](PROJECT_STATUS.md) (this file supersedes it as the
technical reference — PROJECT_STATUS is a log, this is the map).

VocalPay exists as **two separate builds** from one shared design: a server-backed **web app** (real payments,
real database, a self-hosted LLM chatbot) and a fully **offline Android APK** (everything local, no network,
mocked payments). They share the same UI code and the same product decisions, but almost nothing else — the
offline build has its own data layer, its own payment engine, and its own native plugins. That split runs
through this whole document.

---

## 1. Core idea: a deterministic pipeline, with an LLM layered on top, never inside

Every payment goes through the same four stages, in order, and nothing skips a stage:

```
parse (regex, not an LLM) → policy decision → confirmation (phrase or PIN) → execute (Stripe)
```

This is the single most important architectural decision in the project. An LLM can *inform* a user —
explain a rule, look up a balance, summarize spending — but nothing in this app lets an LLM *authorize* a
transfer. The parser is a hand-written regex engine, not a model call, specifically so that "why did this
payment go through" always has a traceable, non-probabilistic answer. This was an explicit design choice made
mid-project when asked to add "agentic payment execution" — declined on safety grounds rather than built.

Every step along the way — command received, intent parsed, payee looked up, decision made, confirmation
created/approved, payment executed — is written to an **append-only, hash-chained audit log**
(`audit.py`): each event's hash includes the previous event's hash, so the whole chain can be
re-verified end-to-end (`GET /audit/{session_id}/verify`) and any tampering would break the chain.

---

## 2. Web app backend

| Piece | Technology | Why |
|---|---|---|
| API framework | **FastAPI** (`main.py`, ~1700 lines) | Async Python, typed request models via Pydantic, serves the static frontend from the same process/origin (no CORS to manage in production) |
| Database | **PostgreSQL**, via **psycopg3** (`db.py`) | Neon (serverless Postgres) in production, Docker Postgres 16 locally (`docker-compose.yml`, port 5433) |
| DB layout | **Per-domain schemas**, not one flat `public` schema | `payments`, `directory`, `services`, `passkeys`, `memory` — each table lives in the schema for its domain (`db.py`'s `TABLE_DOMAIN`). Startup migrates any pre-existing `public` tables into their domain schema automatically, data intact |
| Payments | **Stripe** (test mode), `stripe_service.py` | Real Stripe Connect *destination charges* — a payment to a contact creates a real (test-mode) Connect account for them and transfers funds to it, so "did the money actually arrive" is verifiable against *their* Stripe balance, not just "was the payer's card charged" |
| Scheduling | **APScheduler** (`BackgroundScheduler`) | Drives recurring payments in-process — no external cron needed. Proper startup/shutdown lifecycle so it doesn't leak a thread per test run |
| Passkeys | **py_webauthn** (`passkey_service.py`) | FIDO2/WebAuthn as an alternative step-up confirmation alongside the PIN |
| Testing | **pytest**, 119 tests (`tests/test_vocalpay.py`) | Every test runs against a real, isolated Postgres schema (created and dropped per test), not a mock DB |

### The payment pipeline, module by module
- **`intent_parser.py`** — regex-based natural-language parser. Understands several phrasings ("Pay 12 to
  John", "Send John 12 aud", self-correction — "Pay 20, no 30 to Smith" — last amount wins), not one rigid
  template.
- **`financial_slang.py`** — normalizes amount slang ("a fiver" → 5, "5 grand" → 5000, "20 bucks" → 20)
  *before* the parser's digit-only amount regex runs. Deliberately narrow: no general number-word parsing
  ("twenty"), which is genuinely ambiguous in a payment command (twenty dollars, or someone named Twenty?).
- **`policy.py`** — pure, deterministic decision function: `PROCEED` / `STEP_UP` (PIN required over the $50
  cap) / `CLARIFY` (ambiguous payee) / `BLOCK` (unknown payee, no PayID on file).
- **`payid_directory_repo.py`** — simulates an external PayID network directory (200 deterministic
  mobile+email entries) separate from the user's own saved contacts; paying a registered-but-unsaved PayID
  works immediately (auto-provisions a receiver), matching how real PayID networks behave.
- **`bpay_parser.py` / `bpay_repo.py`** — a second payment rail (biller code + CRN, Luhn/Mod10 check-digit
  validation), settlement simulated since no real BPAY test network exists.
- **`recurring_parser.py` / `recurring_repo.py`** — natural-language recurring payments ("Pay Sam 15 every
  Friday", "Schedule 50 on the 1st of the month"), with calendar-correct month math (not a naive +30 days,
  which drifts and mishandles month-end).
- **Duplicate-payment guardrail** (in `main.py`, backed by `transactions_repo.find_recent_duplicate`) — the
  same payee + same amount within 5 minutes triggers a clarification instead of silently going through; a
  bypass word ("again") lets a genuine repeat through. Deterministic, against the real transaction log — not
  an LLM's judgment call.

---

## 3. The multi-agent support chatbot

**LangGraph + LangChain**, talking to **Ollama** (self-hosted Llama 3.1 8B) — no cloud LLM API anywhere in
this project. `support_chat.py` builds one `StateGraph` containing **four specialist agents**, each with its
own system prompt and only the tools relevant to its domain, instead of one generalist agent holding every
tool at once:

- **account_agent** — contacts, payment rules, recurring schedules
- **transactions_agent** — payment history, a transaction's status, a contact's real Stripe receiver balance,
  a spending summary (`SUM` over the real ledger, grouped by rail)
- **bills_agent** — BPAY billers, service-provider invoices
- **general_agent** — conversational fallback, no tools

**Routing** is a deterministic keyword pass first (zero extra model call for the common case), falling back to
one short LLM classification call only for ambiguous phrasing — that fallback's result is a plain return
value, never written into the persisted conversation, so it can't pollute the chat history.

Conversation memory is **per-user, in Postgres** (`langgraph-checkpoint-postgres`), so a thread survives a
process restart and a mid-conversation switch between specialists.

---

## 4. Web frontend

Plain **HTML/CSS/JavaScript** — no framework, no build step, no bundler. **Tailwind** is loaded from a CDN for
the web build (the offline build compiles it statically — see below, since an APK can't phone a CDN).
`main.py` serves it directly as static files from the same origin as the API.

- **Voice input runs entirely in the browser** — **Whisper** via **`@xenova/transformers`** (ONNX Runtime
  Web/WASM), so audio never leaves the device even in the server build.
- **Accessibility**: ARIA live regions for screen-reader announcements, a high-contrast mode, adjustable text
  size, and the whole UI is `axe-core`-scanned for violations as part of its own test pass.
- **Private Mode**: sensitive values (amounts, names) render blurred behind `tabindex`/`role="button"` until
  tapped, with an idle auto-lock.

---

## 5. The offline Android app (Capacitor)

A **Capacitor 8** WebView wrapper around the *same* frontend code, forked into `mobile/www/`, extended so it
needs **zero network access** at runtime. This is architecturally the most involved part of the project —
every native integration here hit a real build failure that had to be diagnosed and fixed (documented in
`mobile/README.md` and in git history), because this environment has no emulator or physical device to test
against; everything had to be verified by what the build toolchain and a desktop browser could prove.

### Local data: IndexedDB, not SQLite
`mobile/www/offline/idb.js` + `offline-engine.js` is a full **from-scratch port of the backend's business
logic** to JavaScript, running against the browser's own IndexedDB instead of Postgres — payees, PayIDs, the
directory, transactions, confirmations, recorded chat messages, and per-thread "hidden" flags. **SQLite was
considered and deliberately rejected**: a third native plugin after two others (contacts, text-to-speech) each
caused real Gradle build failures, for a benefit IndexedDB already provides (genuine on-device persistence,
no native code).

### Native plugins (each chosen deliberately, not just "the first result")
| Plugin | Does | Note |
|---|---|---|
| `@capacitor-community/contacts` | Reads the phone's own address book ("Import from phone contacts") | |
| `@capacitor-community/text-to-speech` | Spoken replies | Android's WebView exposes `window.speechSynthesis` but doesn't actually implement it on most devices — this wraps the real native TTS service instead |
| `@aparajita/capacitor-biometric-auth` | Fingerprint/face/PIN gate for Private Mode's hidden threads | Chosen over the more commonly-suggested `capacitor-native-biometric`, which still declares Capacitor 3 as its peer dependency and hasn't shipped in over a year — this one matches the installed Capacitor 8 exactly |

Two of these needed their actual public JS method bundled with **esbuild** into a single
self-contained file (`vendor/biometric-auth.js`) rather than called via the raw native bridge, because their
real public method doesn't match the native-exposed method name — confirmed by reading the plugin's own
Java source, not assumed.

### On-device Whisper, fully offline
The same Whisper-via-transformers.js approach as the web build, but with the model weights and the ONNX
WASM runtime **bundled into the APK itself** (`mobile/www/models/`, `mobile/www/vendor/`) instead of fetched
from Hugging Face/jsDelivr at runtime — so voice input works with the device in airplane mode.

### Per-contact chat threads
A WhatsApp-style thread per contact (`mobile/www/app.js`'s thread-list view), each with persisted history in
IndexedDB, a search bar, and a dedicated "Private Chats" section (a fixed row below the scrollable list, not
mixed into it) gated behind the biometric check — including a "global shortcut": paying a hidden contact's
thread from anywhere else in the app still interrupts with the biometric prompt before showing any payment
detail.

### Build toolchain quirks worth knowing about (all documented in `mobile/README.md`)
- **Windows + Gradle + a space in the project path** (`Text based Bot`) causes a real, reproducible
  `java.io.IOException: Invalid file path` — worked around by building from a real copy at a space-free path
  (`C:\vocalpay-mobile`), **not** a symlink/junction (Java resolves those back to the real, spaced path for
  canonical-path operations, so the bug survives a junction).
- A literal `--` inside an XML comment is invalid XML and fails AAPT2's manifest/resource merge — hit twice
  this project, in two different files.
- A native plugin can require manifest permissions, themes, or string resources it doesn't ship itself
  (`AppTheme.Transparent`, `title_activity_auth_activity`) — these fail the build with a resource-resolution
  error if not declared in the app's own `res/`.

---

## 6. Deployment

- **Render** (`render.yaml`) — the web app, as a single free-tier service serving both the API and the static
  frontend from one origin/URL.
- **Neon** — serverless Postgres for production; the same `db.py`/schema code runs against it and against
  local Docker Postgres unchanged.
- **Ollama** is explicitly *not* hosted on Render (the free tier has nowhere near enough RAM) — `OLLAMA_BASE_URL`
  must point at a self-hosted Ollama reachable from the internet, or the chatbot alone (not payments) shows a
  clear connection error.
- The Android build has no deployment step — it's a debug APK, sideloaded (`adb install`), not published to a
  store.

---

## 7. Testing strategy (the actual methodology used throughout the project)

Four levels, used on every non-trivial change, not just at the end:

1. **Automated suite** — `pytest`, 119 tests, each against a real isolated Postgres schema.
2. **Live API checks** — a real `uvicorn` process, hit with real HTTP requests, for flows that are easy to get
   subtly wrong in a mock (Stripe test-mode charges, the audit hash chain, the duplicate guardrail's exact
   wording).
3. **Real-browser checks** — **Playwright**, driving actual Chromium against the app (never just reading the
   code and assuming it works), plus **axe-core** accessibility scans — including deliberately forcing
   transient states (a hover, a locked screen, a denied biometric prompt) that a static review would miss.
4. **Build integrity** — for the Android app specifically, confirming a change is actually present inside the
   packaged `.apk`'s assets, not just in the source tree that was edited.

What this project is explicit about **not** being able to verify in this environment: a real device's
biometric prompt, actual spoken TTS audio, real microphone recording, a real screen reader (TalkBack), and a
live LLM call when system memory is too constrained to safely load one. Those gaps are stated, not assumed
away.
