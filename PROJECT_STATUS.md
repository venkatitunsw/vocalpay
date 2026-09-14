# VocalPay — Project Status

_A text/voice-driven payment assistant: FastAPI + SQLite + Stripe (test mode) backend, with a chat-style
web frontend._

## 1. What it is

VocalPay takes a natural-language payment command (e.g. `"Pay 12 to John for dinner"`), parses it into a
structured intent, runs it through a risk/policy engine, requires the appropriate confirmation (typed phrase
or PIN), and — once confirmed — executes the charge through Stripe's test API. Every step is written to a
tamper-evident, hash-chained audit log. A single-page chat UI ([frontend/](frontend/)) drives the whole flow
against the API.

For payees added as full **contacts** (name + phone/PayID), payment execution moves funds to a real,
separate Stripe Connect test account belonging to that contact — so a "succeeded" payment can be backed by
**the receiver's own Stripe balance actually increasing**, not just the payer's card being charged. See §6a.

## 2. Tech stack

- **Backend:** FastAPI 0.129.0 + Uvicorn (ASGI server), Pydantic 2.12.5, raw `sqlite3` (no ORM)
- **Payments:** Stripe SDK 14.3.0, test mode, off-session confirmed PaymentIntents, idempotency-keyed
- **Frontend:** Static HTML/CSS/vanilla JS, Tailwind via CDN, no build step, no framework
- **Secrets:** `.env` file (`STRIPE_SECRET_KEY`), loaded via `python-dotenv`
- **Hashing:** `hashlib`/`hmac` (PBKDF2-HMAC-SHA256 for PINs, SHA-256 for the audit chain)
- **Testing:** `pytest` + FastAPI `TestClient` (`httpx`), isolated per-test SQLite DB

Full pinned dependency list in [requirements.txt](requirements.txt) (now UTF-8; was accidentally UTF-16 from
a PowerShell `pip freeze >` redirect — fixed).

## 3. Data model (SQLite schema)

Defined in [db/schema.sql](db/schema.sql), applied on startup via `init_db()` in [db.py](db.py):

| Table | Purpose | Key columns |
|---|---|---|
| `users` | Account + PIN hash | `user_id`, `name`, `pin_hash` |
| `payees` | Saved payment recipients (nickname → identifier) | `payee_id`, `user_id`, `nickname`, `type` (`payid`/`stripe`), `identifier`, `phone_number`, `stripe_connected_account_id` |
| `payment_methods` | Tokenized cards (never raw card data) | `pm_id`, `user_id`, `stripe_customer_id`, `stripe_payment_method_id`, `is_default` |
| `transactions` | Logical record of a payment attempt | `txn_id`, `session_id`, `user_id`, `amount_cents`, `currency`, `payee_id`, `status`, `stripe_payment_intent_id`, `stripe_transfer_id`, `destination_account_id`, `receiver_confirmed_at` |
| `confirmations` | Pending step-up confirmation for a transaction | `confirmation_id`, `txn_id`, `required_confirmation`, `pin_attempts`, `max_attempts`, `expires_at`, `status` |
| `audit_events` | Append-only, hash-chained event log | `event_id`, `session_id`, `ts`, `event_type`, `payload_json`, `prev_hash`, `event_hash` |
| `payid_directory` | Mock external PayID registry (200 seeded rows) — see §4a | `phone_number` (PK, digits-only), `display_number`, `registered_name` |
| `chat_messages` | Support chatbot conversation history, per-user — see §2c | `message_id`, `user_id`, `role` (`human`/`ai`), `content`, `created_at` |

`payees.is_contact` (new, default `1`): `1` = deliberately saved contact; `0` = auto-provisioned from a direct
PayID payment, not yet saved — still fully payable and trackable, just hidden from the Setup contacts list
until saved (§4a).

`phone_number`/`stripe_connected_account_id` (on `payees`) and `stripe_transfer_id`/`destination_account_id`/
`receiver_confirmed_at` (on `transactions`) were added for the receiver-evidence feature (§6a). [db.py](db.py)
runs a small idempotent migration (`_run_migrations`) on every startup that `ALTER TABLE ADD COLUMN`s these
into an already-existing `vocalpay.db` — safe to run repeatedly, only adds what's missing.

Indexes on `payees(user_id, nickname)`, `audit_events(session_id, ts)`, `transactions(user_id, created_at)`,
`confirmations(txn_id)`.

Single seeded/demo user: `demo-user` (name "Demo User", PIN `1234`), created on startup by
`ensure_demo_user()` in [users_repo.py](users_repo.py). **All flows operate against this one hardcoded
user** — there is still no auth/login.

## 2a. Flexible intent parsing — new

[intent_parser.py](intent_parser.py) no longer matches one rigid regex template — it resolves the *meaning*
of the command through a small set of prioritized rules, so several real phrasings work without the user
having to match an exact format:

- **Target/payee** is resolved by trying trigger phrases in order: `"to <target>"` first (e.g. `"Pay 12 to
  John"`), then `"on <target>['s] account"` (e.g. `"Pay 30 AUD on Alice account"` / `"...on Alice's
  account"`), then a bare `"for <target>"` as a last resort (e.g. `"Pay for John 12 dollars"`). A trailing
  `"'s account"`/`"account"` is stripped from whatever was captured so the payee name itself stays clean.
- **Amount** is taken from the *last* number mentioned before the target trigger, falling back to scanning
  after it if none appears before. Taking the last number (rather than the first) is what makes a
  self-correction like `"Pay 20, no 30 to Smith"` resolve to **30**, without the parser needing to recognize
  "no" as a correction word specifically — it naturally falls out of "last-mentioned wins."
- **Note** is `"for <text>"` appearing *after* the resolved target (so it doesn't get confused with a bare
  `"for <target>"` payee match).
- The target group also accepts digits/spaces, so a PayID (`"Pay 12 to 0412 345 678"`) resolves the same way
  a name would (see §4a).
- **No preposition at all** — `"Send John 12 aud"` / `"Pay Smith 20"` — is a fourth fallback: when none of
  `to`/`on account`/`for` appear, the target is whatever sits between the verb and the first number in the
  string, and the amount is resolved from that point onward exactly like the other paths (still "last number
  wins", so `"Send John 12, no 15 aud"` correctly resolves to 15).

Still requires the sentence to open with `pay`/`send`/`transfer` — everything after that is now
format-tolerant. Covered by unit tests in [tests/test_vocalpay.py](tests/test_vocalpay.py) (`test_parser_*`)
directly against `parse_text_command()`, plus one end-to-end test through `/command/text`.

## 2b. Persistent storage in production (Turso) — new

Render's free plan has no persistent disk: every redeploy, and every cold start after the free service spins
down from ~15 minutes of inactivity, is a brand-new container with a fresh, empty local filesystem — so the
SQLite file used to reset to just the seeded demo state (demo user, PIN `1234`, 200-entry PayID directory) on
every one of those events, losing any contacts/payment methods/transactions added in between.

Fixed by making [db.py](db.py) storage-backend-aware:

- **Locally and in tests**: `get_conn()` behaves exactly as before — a real `sqlite3.Connection` against the
  local `vocalpay.db` file. Nothing about local dev or the test suite changed.
- **In production**: when `TURSO_DATABASE_URL` (and `TURSO_AUTH_TOKEN`) are set, `get_conn()` instead returns
  `_LibsqlConn`, a thin wrapper around a single shared [Turso](https://turso.tech) (libSQL) client for the
  whole process — Turso is SQLite-wire-compatible, so no SQL anywhere else in the codebase needed to change.
  The wrapper adapts the four methods every repo file actually calls (`execute`/`executemany`/`executescript`/
  `commit`/`close`) and converts result rows to plain `dict`s so `dict(row)` and `row["col"]` keep working
  unchanged everywhere. `commit()`/`close()` are no-ops — libsql commits each statement as it runs, and the
  client is a long-lived shared resource rather than something opened and torn down per call the way a local
  SQLite connection is.
- `executescript()` (used once, by `init_db()` to apply `schema.sql`) strips `--` line comments before
  splitting on `;`, since libsql needs each statement executed individually and schema.sql's comments
  occasionally contain a literal `;` themselves (e.g. `"1 = saved contact; 0 = auto-provisioned"`), which a
  naive split would otherwise cut in the wrong place.
- Free tier, indefinitely: Turso's free plan (500 databases, several GB storage) needs no credit card and
  doesn't expire the way some "free trial" database offers do.
- `render.yaml` declares `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` as `sync: false` env vars (set manually in
  the Render dashboard, never committed) alongside `STRIPE_SECRET_KEY`.

## 2c. Support chatbot — LangChain + Gemini, memory across sessions — new

A second, separate pipeline alongside the deterministic payment flow: when a chat message *doesn't* parse as
a payment command (`/command/text` returns `ok:false` with no `decision` — i.e. genuinely unparseable, not a
policy CLARIFY/BLOCK), the frontend automatically hands it to `POST /support/chat` instead of showing a raw
parser error. This is a deliberate split, not a convenience shortcut:

- **Payments stay 100% deterministic.** The support agent has no ability to create a transaction, confirm a
  PIN, or touch `/pay/execute` — it's not given those as tools at all. If asked to pay someone, its system
  prompt instructs it to tell the user to type a payment command instead ("Pay 12 to John"), which still goes
  through the existing regex parser → policy engine → PIN/CONFIRM flow (§4) untouched. This avoids the real
  risk of an LLM misreading an amount or payee in a system that moves money.
- **Grounded, not hallucinated.** [support_chat.py](support_chat.py) gives the agent five read-only tools —
  `list_contacts`, `recent_transactions`, `transaction_status`, `check_receiver_balance`, and
  `explain_payment_policy` — each a thin wrapper over the same repo functions the rest of the app uses
  (`payees_repo`, `transactions_repo.list_recent_transactions()` (new), `stripe_service.get_account_balance()`).
  The system prompt explicitly instructs it never to guess an amount, status, name, or PayID — always look it
  up.
- **Memory persists per-user, not per-session** — a new `chat_messages` table (`db/schema.sql`) stores every
  turn keyed to the (single, demo) user, not the browser session, so the assistant remembers earlier
  conversations even after closing the tab or a Render cold start (once Turso persistence, §2b, is wired up).
  `_load_history()`/`_save_message()` load the last 20 turns as LangChain `HumanMessage`/`AIMessage` objects
  and append the new exchange after each reply.
- **Model**: Google Gemini (`gemini-2.5-flash` by default, overridable via `GEMINI_MODEL`) via
  `langchain-google-genai`, orchestrated with `langchain.agents.create_agent()` (LangChain 1.x's tool-calling
  agent loop). Requires a `GEMINI_API_KEY` env var (from https://aistudio.google.com/apikey) — the agent, and
  the Gemini client inside it, are constructed lazily on first use, and a missing key surfaces as a clear
  `{"ok": false, "error": "Missing GEMINI_API_KEY..."}` rather than crashing the app or the endpoint.
- Every turn is also audit-logged (`SUPPORT_CHAT_MESSAGE`/`SUPPORT_CHAT_REPLY`/`SUPPORT_CHAT_FAILED`), same as
  every other action in the app.
- `render.yaml` declares `GEMINI_API_KEY` as a `sync: false` env var alongside the others.

## 3a. Duplicate contact names — same person vs. different person — new

Two different PayIDs can legitimately resolve to the same display name — either genuinely (a home and a work
number for one person) or coincidentally (two different people who happen to share a name; the 200-entry mock
directory's small name pool makes this common in testing). Previously this surfaced as a confusing,
indistinguishable `CLARIFY: Multiple contacts match "Ava": Ava Hill, Ava Hill.` Fixed on two fronts:

- **CLARIFY is now qualified by PayID** — `main.py`'s `/command/text` name-resolution branch labels each
  candidate as `"Ava Hill (0487 879 135)"` instead of a bare name, and the response also includes
  `candidate_payees` (payee_id/nickname/phone_number) for a UI to render a proper picker instead of parsing
  text. The reason text also now hints at Setup for merging.
- **Explicit resolution at save time** — `payees.linked_contact_id` (new column) marks a payee row as "the
  same person as" another payee_id. Both `POST /payees/add_contact` and `POST /payees/{payee_id}/save_as_contact`
  ([payees_repo.py](payees_repo.py) `find_saved_contact_by_nickname()`) now check whether the nickname being
  saved collides with an already-saved contact (`is_contact=1`) under a *different* number. If so, instead of
  silently creating an ambiguous duplicate, they return a `{"conflict": "duplicate_name", "existing_contact":
  {...}}` response and require an explicit `resolution`:
  - `"same_person"` — links the new row to the existing one (`linked_contact_id`), so future name lookups
    resolve unambiguously (see below), while both rows keep their own independent payee_id/Stripe account —
    past and future transactions to either number stay fully separate and traceable.
  - `"different_person"` — proceeds with a renamed nickname the caller supplies, kept fully unlinked.
  - This also works to retroactively resolve two already-saved contacts that collided before this feature
    existed — calling `save_as_contact` on the second one goes through the same conflict/resolution path.
- **Name resolution collapses linked groups** — `payees_repo.find_payees_by_name()` now runs matches through
  `_resolve_name_matches()`: if every match in a multi-match result shares the same link-group anchor, it's no
  longer ambiguous — it collapses to one representative (preferring the saved-contact row, then the group's
  anchor) and the payment proceeds using that number, with the read-back showing which PayID was used.
- **Frontend**: the Setup "Add contact" form and the confirmation card's "Save contact" action both render an
  inline choice ("Same person — add as another number" / "Different person — rename") on a `duplicate_name`
  conflict, via a shared `renderDuplicateNameConflict()` helper in [frontend/app.js](frontend/app.js). A
  linked contact shows a small "Same person as another saved number" note in the Setup contacts list.
- **Targeting a specific linked number by name**: once two numbers are linked as the same person, paying by
  bare name resolves to one default (a saved contact, or the group's original/anchor row). To pay the *other*
  linked number specifically, [intent_parser.py](intent_parser.py) now recognizes an explicit PayID qualifier
  attached to a name — `"Pay 4 to Ava Hill PayID: 0427 499 675"` or `"Pay 4 to Ava Hill (0427 499 675)"` — and
  resolves by that number instead of the name, which is unambiguous even when the name isn't. (The target
  character class also had to grow to include `:` and parentheses to parse these phrasings at all — without
  that, the whole command previously failed outright with "Could not figure out who to pay.")

## 4. Backend request flow (end to end)

1. **`POST /session/new`** — creates a `session_id` (UUID), logs `SESSION_START`.

2. **`POST /command/text`** `{session_id, text}` — the core pipeline, in [main.py](main.py):
   1. Logs `COMMAND_TEXT_RECEIVED`.
   2. Parses text via `parse_text_command()` ([intent_parser.py](intent_parser.py)) — understands intent
      across several phrasings rather than one fixed template (see §2a). Currency forced to `AUD`. On
      failure, logs `INTENT_PARSE_FAILED`, returns `ok: false`.
   3. Logs `INTENT_PARSED`.
   4. Looks up whether the payee nickname exists for `demo-user`, logs `PAYEE_LOOKUP`.
   5. Runs the policy engine `decide_next()` ([policy.py](policy.py)): amount `<= 0` → **BLOCK**; payee not a
      saved contact → **BLOCK** ("not a saved contact... add them... before paying"); payee saved but has no
      PayID/receiver account on file (e.g. a legacy bare payee) → **BLOCK** ("has no PayID on file..."); amount
      `> 50 AUD` → **STEP_UP** (PIN); else **PROCEED** (typed `CONFIRM`). Logs `DECISION`. **Money can only be
      paid to a resolved contact's PayID** — an unknown name or a payee without a connected account never
      reaches `/pay/execute` at all (previously an unsaved payee was allowed through as a step-up PIN
      confirmation and would execute as a plain, non-destination charge with no real second party — this was
      changed so a "succeeded" payment always implies a specific contact's PayID actually received it).
   6. `CLARIFY`/`BLOCK` stops here — no transaction created.
   7. Otherwise creates a `pending` transaction, logs `TXN_CREATED`.
   8. Creates a `confirmations` row (120s TTL, max 3 PIN attempts), logs `CONFIRMATION_CREATED`.
   9. Returns a spoken-style read-back — includes the payee's PayID (phone number) when known, e.g.
      `"Confirm: Pay AUD 12.00 to John (PayID: 0412 345 678). Required: NORMAL"` — plus a `payee` object
      (`{payee_id, phone_number, has_receiver_tracking}`) so the frontend knows whether this payment will be
      able to produce receiver evidence.

3. **Confirmation:**
   - **`POST /confirm/normal`** `{session_id, confirmation_id, phrase}` — phrase must equal `"CONFIRM"`.
   - **`POST /confirm/pin`** `{session_id, confirmation_id, pin}` — verifies against the PBKDF2 hash
     (`verify_pin`). Wrong PIN increments `pin_attempts`; hitting `max_attempts` (3) rejects the
     confirmation and fails the transaction.
   - **Both endpoints now check `expires_at` before anything else** (see §6, TTL enforcement) — an expired
     confirmation is rejected immediately, regardless of what phrase/PIN was submitted.
   - Every attempt/outcome is logged (`CONFIRM_*_ATTEMPT`, `CONFIRM_*_FAILED`, `CONFIRM_APPROVED`,
     `CONFIRM_REJECTED`, `CONFIRMATION_EXPIRED`).

4. **`POST /pay/execute`** `{session_id, txn_id}`:
   - Requires `confirmed` status (else `PAY_EXECUTE_BLOCKED`).
   - Loads the user's default payment method; fails if none set.
   - Looks up the payee's `stripe_connected_account_id`. If set, calls Stripe's `create_payment_intent()`
     with `destination_account_id` (a **destination charge** — see §6a) and a **deterministic idempotency
     key** (`vocalpay_txn_{txn_id}`) — see §6.
   - Distinguishes Stripe failure types (see §6) — every outcome sets the transaction to a terminal
     `succeeded`/`failed` state and logs `PAY_EXECUTE_SUCCESS` or `PAY_EXECUTE_FAILED` (with an
     `error_type`).
   - On success, if a destination account was used, confirms **receiver-side evidence** (§6a) and returns it
     as `receiver_evidence` in the response (`null` for payees without receiver tracking).

## 4a. Paying by PayID number directly — new

You can now target a payment by PayID number instead of a saved nickname, e.g. `"Pay 34 to 0400777888"` or
`"Pay 34 to 0412 345 678"`. [intent_parser.py](intent_parser.py)'s payee group now accepts digits, so a
phone-number-shaped token parses the same as a name would.

In [main.py](main.py) `/command/text`, `payid_directory_repo.looks_like_payid()` (8-10 digits once
spaces/dashes are stripped) decides whether the typed target is a PayID or a nickname, and the two go through
separate resolution paths:

1. **Own contacts first** — `payees_repo.find_payee_by_phone()` compares digits-only against every saved
   payee's `phone_number`, so formatting doesn't matter. A match resolves exactly like a name match would
   (same PROCEED/STEP_UP policy, same read-back with the contact's real name).
2. **External PayID directory** — if it's not one of your contacts, `payid_directory_repo.lookup_payid()`
   checks a **200-entry mock PayID registry** (table `payid_directory`, seeded once on startup by
   `seed_payid_directory()` — deterministic, idempotent, only fills the table if it's not already at 200 rows).
   This simulates a bank-network-style directory: a number can be a real, registered PayID with a name behind
   it even if you've never paid them before.
   - **Found in the directory, not in your contacts** → matches real PayID networks (and things like Zelle):
     **saving a contact is never a precondition to paying them.** The backend auto-provisions a real (test-mode)
     Stripe Connect receiver account for that PayID on the spot (`create_test_connected_account`) and creates a
     lightweight, non-contact `payees` row for it (`is_contact=0`) — logging `PAYID_AUTO_PROVISIONED`. The
     payment then proceeds through the normal PROCEED/STEP_UP policy exactly as if it were a saved contact,
     because it now has the same receiver tracking a saved contact would. The `/command/text` response's
     `payee.is_saved_contact` is `false` so the frontend can show an optional, non-blocking "Save contact"
     link in the confirmation card — clicking it calls `POST /payees/{payee_id}/save_as_contact`, which just
     flips `is_contact` to `1` (no re-entering name/phone, since both were already resolved from the
     directory). A second payment to the same number reuses the same payee row/account rather than
     re-provisioning.
   - **Not found anywhere** → **BLOCK**, `"<number>" is not a valid, registered PayID.` — same hard stop as an
     unknown name (§4).
   - `GET /payees` (the Setup tab's contacts list) only returns `is_contact=1` rows, so auto-provisioned
     one-off recipients don't clutter it until deliberately saved — but they remain fully payable, and every
     payment to them is still recorded (transaction row + audit trail) regardless of contact-list visibility.

This means a payment can now be authorized three ways — a saved nickname, a saved contact's PayID number, or
a registered-but-unsaved PayID that gets a receiver account provisioned automatically — but never to a number
or name with no verifiable identity behind it. Saving to contacts is purely a convenience for next time.

## 5. Supporting endpoints

- **`POST /payees/add`** `{nickname, type="payid", identifier="demo"}` — bare payee, no receiver tracking.
- **`GET /payees`** — lists `demo-user`'s saved payees (including `phone_number`/`stripe_connected_account_id`).
- **`POST /payees/add_contact`** `{nickname, phone_number}` — the "contacts" flow: creates a payee **and** a
  real test-mode Stripe Connect account for them in one call. See §6a.
- **`POST /payees/seed_demo_contacts`** — one-click creates a small variety of demo contacts (Alice
  Wonderland, Bob Marley, Charlie Chaplin), each with its own Connect account; skips any that already exist.
- **`GET /payees/{payee_id}/balance`** — on-demand receiver-side evidence: queries that contact's own Stripe
  balance directly, independent of whatever was recorded at execute-time.
- **`POST /payment_methods/add`** `{label, stripe_customer_id?, stripe_payment_method_id}` — marks the new
  method default, unsetting any prior default.
- **`GET /payment_methods`** — lists `demo-user`'s payment methods.
- **`POST /payment_methods/seed_test_card`** — demo/test-mode only: server-side creates a Stripe Customer,
  attaches Stripe's canned test Visa (`pm_card_visa`), and saves it as the default payment method. Lets the
  frontend offer a one-click "add a test card" action without collecting real card data or embedding
  Stripe.js/Elements. Implemented in `stripe_service.create_test_payment_method()`.
- **`GET /health`** — liveness + DB path.

## 6. Backend hardening (Track B — done)

- **TTL expiry enforcement** ([confirmations_repo.py](confirmations_repo.py) `is_confirmation_expired()`,
  wired into both `/confirm/normal` and `/confirm/pin` in [main.py](main.py)): an expired confirmation is
  set to `expired`, its transaction to `failed`, and a `CONFIRMATION_EXPIRED` audit event is logged —
  previously `expires_at` was stored but never checked.
- **Stripe idempotency keys** ([stripe_service.py](stripe_service.py) `create_payment_intent()` now accepts
  `idempotency_key`; `/pay/execute` passes `vocalpay_txn_{txn_id}`) — a retried execute call on the same
  transaction can no longer double-charge.
- **Typed Stripe error handling** in `/pay/execute`:
  - `stripe.error.CardError` (declined card) → transaction `failed`, user-facing decline message.
  - `stripe.error.APIConnectionError` / `RateLimitError` (transient/network) → transaction `failed`,
    "temporarily unavailable, please retry" message. (Originally left `confirmed` so a client could
    silently retry; changed to `failed` — a stalled transaction should surface as a clear failure rather
    than sit in limbo, since `/pay/execute` refuses to run again once a transaction leaves `confirmed`
    anyway.)
  - Generic `stripe.error.StripeError` / unknown `Exception` → transaction `failed`, generic message.
  - Every branch logs `PAY_EXECUTE_FAILED` with an `error_type` for audit/debugging.
- **CORS** — `CORSMiddleware` added to [main.py](main.py) (dev-permissive, `allow_origins=["*"]`) so the
  frontend, served from a different port, can call the API. **Tighten this to a real origin list before any
  non-local deployment.**

## 6a. Receiver evidence (Stripe Connect) — new

**The problem this solves:** originally, "payment succeeded" only meant the payer's card was charged — the
payee name was purely a label in our own database, never sent to Stripe. A malicious or buggy backend could
report `succeeded` without a real second party ever receiving anything. There was no way to prove a specific
person actually got the money.

**How it works now, for payees added as "contacts":**
1. `POST /payees/add_contact` calls `stripe_service.create_test_connected_account()`, which creates a real
   Stripe **Custom Connect account** (country `AU`) using Stripe's documented test-mode "magic values" —
   `dob: 1902-01-01`, `id`/address `address_full_match`, phone `0000000000`, AU test bank BSB `110000` /
   account `000123456` — so the account becomes **instantly verified and active** (`transfers` capability)
   via a single API call, with no hosted onboarding and no manual dashboard steps per contact. Verified live
   against the real Stripe test API (see below).
2. The payee row stores that `stripe_connected_account_id` plus the contact's `phone_number` (shown as their
   PayID in the read-back and confirmation card).
3. `POST /pay/execute` checks whether the payee has a connected account. If so, `create_payment_intent()` is
   called with `transfer_data.destination` set — a **destination charge**: the payer's card is charged and
   Stripe atomically creates a `Transfer` moving the funds to the payee's own account, in a single request.
4. After the charge succeeds, the backend confirms receipt by reading **the payee's own Stripe balance**
   (`stripe.Balance.retrieve(stripe_account=...)`) — not our database, Stripe's live record for that specific
   account. This is retried briefly (up to 4x, ~0.75s apart) because Stripe attaches the `Transfer` and
   updates the destination balance a moment *after* the charge response, even in test mode — an immediate
   single check can read a false `0`. Confirmed empirically: a same-turn `Transfer` lookup right after
   `PaymentIntent.create()` initially raised `AttributeError` because the field wasn't populated yet.
5. The transaction is stamped with `destination_account_id`, `stripe_transfer_id`, `receiver_confirmed_at`,
   and the audit log gets `RECEIVER_BALANCE_CONFIRMED` (or `RECEIVER_BALANCE_PENDING` if the retries ran out
   — rare in testing, but handled honestly rather than silently reported as confirmed).
6. `GET /payees/{payee_id}/balance` lets anyone re-check that contact's Stripe balance at any later time,
   independent of a specific transaction — real, standing proof, not a one-time snapshot.

**Live-verified, not just unit-tested:** this was proven against the user's actual Stripe test account
end-to-end — creating two different contacts (John, Bob Marley), paying each, and confirming their balances
increased *independently* (John's stayed at `1200` cents while Bob's separately showed `800` cents after his
own payment). Required the user to enable Stripe Connect via `dashboard.stripe.com/connect` first — Connect
must be turned on per-account before any connected accounts can be created; this is a one-time dashboard
step, not something an API key alone can do.

**Payees without a connected account** (added via the old `/payees/add`, or before this feature existed) can
no longer be paid at all — the policy engine (§4 step 5) now **BLOCK**s any payment whose resolved payee has
no `stripe_connected_account_id`/PayID, before a transaction is even created. This closes the gap where a
"succeeded" payment only proved the payer's card was charged, with no real second party — every successful
payment must now resolve to a real contact's PayID.

### Audit log (tamper-evidence) — unchanged design, still solid

Implemented in [audit.py](audit.py): each event's hash is
`SHA256(canonical_json({event_id, session_id, ts, event_type, payload, prev_hash}))`; `prev_hash` chains to
the previous event for that session. `GET /audit/{session_id}/verify` recomputes the whole chain and reports
exactly where/why it's broken if tampered.

## 7. Automated tests (new)

[tests/conftest.py](tests/conftest.py) + [tests/test_vocalpay.py](tests/test_vocalpay.py), run with
`pytest`. Each test gets an isolated SQLite file (`db.DB_PATH` monkeypatched per test) and stubbed Stripe
calls (`create_payment_intent`, `create_test_payment_method`, `create_test_connected_account`,
`get_account_balance`, `stripe.Charge.retrieve`) — **no real Stripe calls happen in the suite**. 16 tests,
all passing:

1. `test_happy_path_full_flow` — session → command → normal confirm → execute → audit verify, all green.
2. `test_wrong_pin_lockout` — 3 wrong PINs rejects the confirmation and locks it out.
3. `test_correct_pin_confirms` — correct PIN on a step-up (new payee) confirms.
4. `test_expired_confirmation_is_rejected` — a confirmation created with a negative TTL is rejected and
   logs `CONFIRMATION_EXPIRED`.
5. `test_audit_chain_detects_tampering` — directly mutating a stored `payload_json` row makes
   `/audit/{id}/verify` report `event_hash mismatch`.
6. `test_execute_handles_card_decline` — simulated `CardError` → `ok: false`, transaction `failed`.
7. `test_execute_handles_transient_failure` — simulated `APIConnectionError` → `ok: false`, transaction
   `failed`.
8. `test_setup_endpoints_list_and_seed` — `GET /payees`/`GET /payment_methods` start empty, adding a payee
   and seeding a (stubbed) test card both show up in the respective lists.
9. `test_seed_test_card_handles_stripe_error` — a simulated `StripeError` during seeding returns
   `ok: false` and leaves `payment_methods` untouched (no partial row written).
10. `test_add_contact_creates_payee_with_connect_account` — `/payees/add_contact` stores the mocked
    connected-account id and phone number on the payee row.
11. `test_add_contact_requires_nickname_and_phone` — both fields are mandatory.
12. `test_add_contact_handles_stripe_error` — a simulated `StripeError` during account creation returns
    `ok: false` and leaves no partial payee row.
13. `test_seed_demo_contacts_creates_variety_and_skips_duplicates` — first call creates all 3, second call
    creates 0 and reports all 3 as skipped.
14. `test_pay_to_contact_returns_receiver_evidence` — full flow to a contact returns
    `receiver_evidence.confirmed: true` with the mocked destination account, transfer id, and balance; the
    read-back includes the PayID; `RECEIVER_BALANCE_CONFIRMED` is logged.
15. `test_pay_to_plain_payee_has_no_receiver_evidence` — a payee added via the old `/payees/add` still pays
    successfully but `receiver_evidence` is `null`.
16. `test_balance_endpoint_rejects_payee_without_connect_account` — `/payees/{id}/balance` on a
    non-contact payee returns a clear error instead of a confusing empty result.

## 8. Frontend (Track A — new)

Location: [frontend/](frontend/) — `index.html`, `app.js`, `styles.css`. No build tooling, no npm install;
Tailwind is pulled from a CDN `<script>` tag. Opens directly against the FastAPI backend at
`http://127.0.0.1:8000` (change `API_BASE` at the top of `app.js` to point elsewhere).

**F1 — Chat interface & session lifecycle**
- On load, calls `POST /session/new`, stores `session_id`, shows a short id badge in the header, and posts a
  greeting bubble.
- User messages and assistant text responses render as chat bubbles in a scrolling feed
  (`POST /command/text` drives this).
- Parse failures and `CLARIFY`/`BLOCK` decisions render as a red error bubble instead of raw JSON.

**F2 — Step-up interactive elements**
- A successful `/command/text` response renders an **interactive confirmation card** (not raw JSON) showing
  the amount/payee/note, plus the payee's **PayID** (phone number) when known. If the payee exists but has no
  receiver tracking, an inline amber note says so explicitly ("payment will succeed but can't be proven to
  reach them") rather than staying silent about it.
- `required_confirmation: "normal"` → a text field pre-styled for `CONFIRM` + a Confirm button
  (`POST /confirm/normal`).
- `required_confirmation: "pin"` → four separate auto-advancing digit boxes + Confirm button
  (`POST /confirm/pin`), with backspace-to-previous-box and clear-on-wrong-PIN behavior.
- Wrong PIN shows inline feedback and keeps the card open (up to 3 tries); hitting the lockout disables the
  card with the backend's own message; an expired confirmation surfaces the same "Confirmation expired"
  message from §6.

**F3 — Payment execution, receiver evidence & audit visualizer**
- Once a confirmation is approved, the card swaps to a "Pay now" button.
- Clicking it shows an inline "Authorizing through Stripe…" spinner state, then replaces itself with a
  success (`final_status`, Stripe PaymentIntent id) or failure (error + details) result — all inline in the
  same card, no page reload.
- **Receiver evidence** (§6a): when the response includes `receiver_evidence`, the result card shows a
  second block — "Received by \<name\>" with the live amount now in *their* Stripe balance and the
  destination account id / transfer id, styled green when `confirmed: true` and amber ("receipt not yet
  confirmed — check again shortly") in the rare case the balance hadn't updated in time. When
  `receiver_evidence` is `null` (a non-contact payee), an explicit gray note explains why no proof is
  available instead of just omitting it.
- A collapsible **side panel** (toggle buttons in the header) holds two tabs sharing one drawer:
  - **Audit Log** — fetches `GET /audit/{session_id}/events` + `GET /audit/{session_id}/verify` and
    re-fetches automatically after every user action (command sent, confirmation attempted, payment
    executed) — showing each event's type, timestamp, and a truncated hash, plus a verified/broken chain
    badge driven by the real hash-chain verification endpoint (not a simulation).
  - **Setup** — lists `demo-user`'s contacts and payment methods. Each contact row shows its PayID and, if
    it has a Connect account, a "receiver tracked" badge and a **"Check receiver balance"** link
    (`GET /payees/{id}/balance`) that fetches and displays that contact's live Stripe balance on demand — you
    can verify receipt independent of any specific payment, at any later time. The "add contact" form takes
    a name **and** phone number and calls `POST /payees/add_contact` (creates the payee + their Connect
    account together); a **"+ Seed a variety of demo contacts"** button one-click creates Alice Wonderland,
    Bob Marley, and Charlie Chaplin, each with their own separately-trackable receiver account — this
    satisfies "create a variety of individuals to track whether money is received." The payment-methods half
    still uses `POST /payment_methods/seed_test_card` (§5) — no raw card fields anywhere in the frontend.

**Voice-ready foundation**
- The mic button uses the browser's native `SpeechRecognition`/`webkitSpeechRecognition` API (no external
  library) to transcribe speech into the text box for the user to review before sending — a real,
  working implementation, not just a placeholder, though it degrades gracefully (button disabled with a
  tooltip) in browsers without support (e.g. Firefox).
- A pulsing "listening" ring and waveform-bar CSS keyframes are in place in
  [styles.css](frontend/styles.css) for a future streaming-audio upgrade.

## 9. Known gaps / demo shortcuts (not yet productionized)

- Single hardcoded `demo-user` — no real auth, login, or multi-user support (frontend has no login screen
  either — it's implicitly "you are demo-user").
- Intent parser is a single rigid regex; no fuzzy matching, multi-intent, or LLM/NLU-based parsing.
- No amount/currency conversion — everything is hardcoded to AUD.
- CORS is wide open (`allow_origins=["*"]`) — fine for local dev, not for any shared/public deployment.
- `.env` holds a live-looking Stripe secret key checked in via a real `.env` file (not `.env.example`) —
  confirm it's **test-mode only** and that `.env` is git-ignored before any commit/push.
- Frontend has no build/bundling, no TypeScript, no component framework, and no automated (e.g. Playwright)
  tests — acceptable for the current MVP scope, worth revisiting if the UI grows.
- Frontend served frontend/index.html directly assumes the backend is reachable at
  `http://127.0.0.1:8000` — no environment-based config yet.
- Setup tab is additive only — no way to remove a payee, switch the default payment method back to an
  older one, or delete a payment method from the UI (or the API — those repo/route functions don't exist
  yet).
- `POST /payment_methods/seed_test_card` is explicitly a demo/test-mode convenience (Stripe's canned
  `pm_card_visa`) — it does not exercise real card collection and should not be mistaken for a production
  card-entry flow; a real deployment would need Stripe.js/Elements or a hosted Payment Element instead.
- `stripe_service.create_test_connected_account()` (§6a) is equally demo/test-mode only — it uses Stripe's
  documented test-mode "magic values" for instant KYC verification, which only work with Custom accounts via
  direct API calls, never with real onboarding. A production version of this feature would need real
  Express/Standard account onboarding (hosted, interactive, subject to actual KYC) per contact — you cannot
  silently create a "verified" real bank-linked account for someone with one API call outside test mode.
- Connect accounts are always created in `country="AU"` regardless of the contact's real location — fine for
  a demo, would need to be a real onboarding input in production.
- Receiver-balance confirmation retries up to 4 times (~0.75s apart, ~3s worst case) inside `/pay/execute`
  before giving up and returning `confirmed: false` — observed necessary because Stripe attaches the
  `Transfer`/updates the destination balance asynchronously, a beat after the charge itself succeeds, even
  in test mode. This adds latency to the payment response; a production system would likely confirm receipt
  via webhook instead of a synchronous poll.
- The working directory is not its own isolated git repository — the nearest `.git` is rooted at the
  Windows user profile folder (`C:\Users\venka`), so `git status`/`git log` here pick up unrelated
  home-directory content. Worth initializing a proper repo scoped to this project folder before committing
  any of this work.

## 10. File map

```
main.py                  FastAPI app + all HTTP routes, CORS, Stripe error handling
models.py                PaymentIntentParsed pydantic schema
intent_parser.py         Regex-based text -> intent parser
policy.py                decide_next() risk/policy engine
security.py              PIN hashing/verification (PBKDF2)
audit.py                 Hash-chained append-only audit log
db.py                    DB connection (SQLite locally, Turso/libSQL in prod — see §2b) + schema init + migrations
db/schema.sql            Table definitions (payees + transactions include receiver-evidence columns)
support_chat.py          LangChain + Gemini support chatbot: read-only tools, per-user DB-persisted memory — see §2c
users_repo.py            Demo user bootstrap
payees_repo.py           Payee lookup/existence checks + get_payee() + find_payee_by_phone() + set_payee_connected_account()
payid_directory_repo.py  200-entry mock external PayID registry: seed_payid_directory(), lookup_payid(), looks_like_payid()
payment_methods_repo.py  Default payment method get/set
transactions_repo.py     Transaction CRUD + set_receiver_evidence()
confirmations_repo.py    Confirmation CRUD + expiry check
stripe_service.py        Stripe init, PaymentIntent creation (idempotency + destination charges), test
                         payment method seeding, test Connect account creation, receiver balance lookup
check_db.py              Dev script: list DB tables
check_pm.py              Dev script: dump payment_methods
create_stripe_pm.py      Dev script: seed a Stripe test card/customer
requirements.txt         Pinned Python dependencies (now UTF-8; includes pytest/httpx/langchain/libsql-client)
vocalpay.db              Local SQLite database file (dev only — prod uses Turso, see §2b)
.env                     STRIPE_SECRET_KEY / TURSO_* / GEMINI_API_KEY (local secrets, keep out of git)
tests/conftest.py        Isolated-DB pytest fixture
tests/test_vocalpay.py   45 integration tests (payments, contacts, PayID, duplicate-name resolution, support chat)
frontend/index.html      Chat UI shell + Setup/Audit tabbed side panel (Tailwind CDN)
frontend/app.js          All frontend logic: session, chat, confirm cards + PayID, pay execution + receiver
                         evidence, audit panel, setup tab (contacts + balance checks), voice
frontend/styles.css      Chat bubble/PIN-box/waveform/spinner styling
```
