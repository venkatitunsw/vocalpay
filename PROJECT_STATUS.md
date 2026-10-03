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
| `passkey_credentials` | FIDO2/WebAuthn passkeys — see §2e | `credential_id`, `user_id`, `public_key_cbor`, `sign_count`, `transports_json`, `label` |
| `bpay_directory` | Mock BPAY biller directory (4 seeded rows) — see §8a | `biller_code` (PK), `biller_name`, `crn_rule`, `min/max_amount_cents` |
| `saved_bpay_billers` | A user's saved billers | `biller_id`, `user_id`, `nickname`, `biller_code`, `crn` |
| `recurring_schedules` | Scheduled/recurring payments — see §8b | `schedule_id`, `user_id`, `payment_rail`, `target_identifier`, `amount_cents`, `cadence`, `next_run_at`, `status` |

`payees.is_contact` (new, default `1`): `1` = deliberately saved contact; `0` = auto-provisioned from a direct
PayID payment, not yet saved — still fully payable and trackable, just hidden from the Setup contacts list
until saved (§4a).

`phone_number`/`stripe_connected_account_id` (on `payees`) and `stripe_transfer_id`/`destination_account_id`/
`receiver_confirmed_at` (on `transactions`) were added for the receiver-evidence feature (§6a). `transactions`
also has `rail` (`"payid"` default, or `"bpay"`), `bpay_biller_code`, `bpay_crn` (§8a); `confirmations` has
`challenge` (§2e, WebAuthn). [db.py](db.py) runs a small idempotent migration (`_run_migrations`) on every
startup that `ALTER TABLE ADD COLUMN`s these into an already-existing `vocalpay.db` — safe to run repeatedly,
only adds what's missing.

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

## 2c. Support chatbot — LangChain + self-hosted Ollama, memory across sessions — new

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
- **Model**: a self-hosted model via Ollama (`qwen2.5:7b-instruct` by default, overridable via
  `OLLAMA_MODEL`), reached over plain HTTP at `OLLAMA_BASE_URL` (default `http://localhost:11434`) using
  `langchain-ollama`'s `ChatOllama`, orchestrated with `langchain.agents.create_agent()` (LangChain 1.x's
  tool-calling agent loop). **No cloud LLM API of any kind is used anywhere in this app** — a deliberate
  choice, not just a default; see §2d for why Gemini specifically was removed. No API key involved: an
  unreachable `OLLAMA_BASE_URL` fails clearly at first use (a connection error) rather than at startup, and
  `LLM_PROVIDER` is no longer a real choice (only `"ollama"` is accepted — kept as a named setting rather than
  hardcoded so a future alternative self-hosted backend could still be added the same way).
- Every turn is also audit-logged (`SUPPORT_CHAT_MESSAGE`/`SUPPORT_CHAT_REPLY`/`SUPPORT_CHAT_FAILED`), same as
  every other action in the app.
- `render.yaml` declares `OLLAMA_BASE_URL`/`OLLAMA_MODEL` as env vars — `OLLAMA_BASE_URL` is `sync: false`
  (you must point it at a real server you run; Render's free plan can't host Ollama itself).

## 2d. Gemini was removed entirely — self-hosted Ollama only

This app briefly used Google Gemini (`langchain-google-genai`) as the support chatbot's default backend, with
Ollama as an opt-in alternative. **Gemini has since been removed completely** — not just switched off by
default — at the user's explicit request to have no cloud LLM API anywhere in the app:

- `langchain-google-genai` is no longer a dependency (`requirements.txt`).
- `support_chat.py`'s `_build_model()` only builds `ChatOllama`; the Gemini branch, `GEMINI_MODEL` constant,
  and `GEMINI_API_KEY` handling are gone, not just unreachable.
- `render.yaml` and `.env.example` no longer reference `GEMINI_API_KEY`.
- A LangChain SDK quirk found and fixed along the way, in case it resurfaces with another provider:
  `langchain_core` message content sometimes arrives as a real list of typed blocks, and sometimes as an
  already-stringified repr of that same list (observed live with Gemini, inconsistent between calls on the
  same model). `_extract_text()` in `support_chat.py` detects and parses the stringified form too
  (`ast.literal_eval`) before pulling out just the text — otherwise raw internal signature/metadata could leak
  into a reply or into persisted chat history. Kept in place since it's cheap insurance against the same
  pattern recurring with Ollama's own response shapes.
- **This means the support chatbot needs you to run a real Ollama server and point `OLLAMA_BASE_URL` at it** —
  there is no fallback cloud provider anymore. Without it, `/support/chat` returns a connection-error message
  instead of crashing; payments (PayID, BPAY, PIN/passkey confirmation) are completely unaffected either way,
  since the chatbot was always a separate pipeline from them (§2c).

## 2e. Passkeys (FIDO2/WebAuthn) — an additional step-up method alongside PIN — new

Exactly as scoped: **PIN keeps working unchanged** (`/confirm/pin`, `/confirm/normal`) — passkeys
(`/confirm/passkey`) are a second, optional way to satisfy the *same* step-up requirement, never a
replacement.

- `passkey_service.py` wraps the `webauthn` (py_webauthn) library: `begin_registration`/`finish_registration`
  for enrolling a new passkey, `begin_authentication`/`finish_authentication` for satisfying a confirmation.
  `passkey_credentials` (new table) stores each credential's public key (COSE/CBOR) and sign count.
- **Challenge binding**: each PIN-equivalent confirmation's WebAuthn challenge is stored on that specific
  `confirmations` row (`challenge` column, new) via `set_confirmation_challenge()` — an assertion is checked
  against the challenge issued for *that* confirmation, not just any valid signature, which is what stops a
  replayed assertion from satisfying a different payment.
- **Cloned-authenticator detection**: a legitimate authenticator's sign count only ever increases. A new
  assertion whose count didn't grow past what's stored (when the stored count was already nonzero) is rejected
  outright as a possible cloned credential — verified with a dedicated test.
- `/confirm/passkey` is accepted on any confirmation that would otherwise accept a PIN (`required_confirmation`
  `"pin"` or `"passkey"`) — a biometric hardware-backed signature is at least as strong an assurance as a
  4-digit PIN.
- **Frontend**: Setup has a "Register a passkey" button (`navigator.credentials.create()`); the PIN
  confirmation card has a "Use a passkey instead" link (`navigator.credentials.get()`) when the browser
  supports `PublicKeyCredential`. Binary fields (challenge, credential ids) are converted between
  base64url (what py_webauthn's `options_to_json()` emits) and `ArrayBuffer` (what the browser API needs) by
  hand in `app.js` — no bundler/npm package in this project, so no `@simplewebauthn/browser`; same conversion,
  written out.
- `WEBAUTHN_RP_ID`/`WEBAUTHN_RP_NAME`/`WEBAUTHN_ORIGIN` env vars must match the serving domain exactly
  (`localhost`/`http://localhost:8001` locally; `vocalpay.onrender.com`/`https://vocalpay.onrender.com` in
  `render.yaml`) — a mismatch is the most common cause of WebAuthn failures.
- **Known limitation**: pending *registration* challenges live in an in-memory dict keyed by user_id
  (`passkey_service._pending_registration_challenges`), not the database — fine for this single-process demo,
  but wouldn't survive a restart mid-registration or multiple server workers. Authentication challenges don't
  have this problem (persisted on the `confirmations` row).

## 2f. Local voice transcription (faster-whisper) — new, additive

`POST /voice/transcribe` (`voice_service.py`) runs speech-to-text locally via `faster-whisper` (CPU, int8,
`tiny.en` by default — `small.en` per the original blueprint spec needs a larger one-time download, set via
`WHISPER_MODEL_SIZE`) instead of a cloud API. **Additive, not a replacement**: the frontend's mic button still
uses the browser's built-in `SpeechRecognition` where available (zero backend load, works today); this
endpoint is the alternative path for a fully local/open-source pipeline.

Includes `normalize_spoken_numbers()` — Whisper transcribes a spoken phone number as digit *words* far more
often than digits ("oh four one two three four five six seven eight" rather than "0412345678"); without
converting runs of 3+ digit-words back to digits, a read-aloud PayID never matches the intent parser's PayID
pattern. Verified against the blueprint's own example input.

**Known limitation**: needs real CPU/RAM (and a one-time model-weight download) that Render's free tier
doesn't reliably have — documented in `render.yaml`, not silently broken. Tested with the model mocked (no
real inference in CI); the pipeline itself (temp file handling, normalization, error surfacing) is real code,
not a stub.

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

## 8a. BPAY rail — new (simulated settlement)

A second payment rail alongside PayID, routed from the same chat input: the frontend sends any message
containing "BPAY" to `POST /bpay/command` instead of `/command/text`. `bpay_parser.py` extracts a biller
name/code, CRN, and amount from free text (e.g. `"Pay Origin BPAY 30 dollars, biller code 111999, reference
79927398713"`); `bpay_repo.py` resolves the biller against a seeded demo directory (`bpay_directory`, 4
billers) and validates the CRN's check digit with a real Luhn/Mod10 implementation (verified against known
test vectors). Missing or invalid slots return a `CLARIFY`-shaped response with what's missing, the same
pattern as the PayID flow's duplicate-name conflicts — for the frontend to prompt for what's still needed.

A valid BPAY command reuses the *exact same* confirmation pipeline as PayID payments (`create_confirmation`,
`/confirm/normal`, `/confirm/pin`, `/confirm/passkey` all work unchanged on a BPAY transaction) — only
`POST /bpay/execute` differs, since there's no Stripe test-mode equivalent for a real BPAY network: settlement
is **simulated** and clearly labeled as such (`note` field in the response, `BPAY_SETTLEMENT_SIMULATED` audit
event with a fabricated `BPAY-SIM-...` reference) rather than presented as a genuine payment. Users can also
save a biller as a favorite (`POST /bpay/billers/save`, Setup tab lists them) for the recurring-payments
feature below.

## 8b. Recurring/scheduled payments — new

`recurring_repo.py` + `POST /recurring/schedules` let a user set up a weekly/fortnightly/monthly payment
against either rail (a saved PayID contact or a saved BPAY biller). There's no cron daemon built into this
app — `POST /recurring/process_due` is the trigger: it finds every active schedule whose `next_run_at` has
passed, creates and **auto-confirms** the transaction (standing authorization was already granted when the
schedule was created, so this skips the interactive CONFIRM/PIN step), executes it through the same
`pay_execute`/`bpay_execute` functions the manual flows use, and advances `next_run_at` by one cadence period
— called on demand for a demo, or wire an external scheduled job (e.g. a Render Cron Job, or any periodic
HTTP caller) to hit it for real recurring behavior.

## 8c. Blueprint coverage — what's done, what's deferred

A large architectural blueprint was supplied covering a near-complete rewrite (open-source local LLM/speech,
FIDO2 passkeys, a new payment rail, an explicit LangGraph state machine, and a conversational edge-case
matrix). Implemented in full except where compute/safety tradeoffs made a deferral the right call — tracked
here rather than silently dropped:

| Blueprint item | Status |
| --- | --- |
| Self-hosted LLM (Qwen 2.5 via Ollama) | **Done** — `LLM_PROVIDER=ollama` (§2d); needs you to run/reach an Ollama server, not usable on the free Render plan as-is |
| Local speech-to-text (faster-whisper) | **Done, additive** — `/voice/transcribe` (§2f); browser `SpeechRecognition` stays the default, lighter path |
| FIDO2/WebAuthn passkeys | **Done, additional to PIN** — §2e, by explicit design choice (PIN was kept, not replaced) |
| BPAY payment rail + CRN validation | **Done, simulated settlement** — §8a (no real BPAY test network exists) |
| Recurring/scheduled payments | **Done, no built-in cron** — §8b (needs an external trigger to actually run unattended) |
| Self-correction ("no 30", "wait make it 35") | **Done** — intent_parser.py now catches a correction stated either before or after the payee |
| Spoken-number normalization ("oh four one two...") | **Done** — `voice_service.normalize_spoken_numbers()` |
| Duplicate-contact-name picker qualified by PayID | **Done** — §3a (built in an earlier round of work) |
| Invalid/negative amount rejection | **Done** — already enforced (Pydantic `gt=0` + policy `BLOCK`) |
| Missing-slot clarification UI (BPAY) | **Done** — `/bpay/command`'s `missing` field, surfaced in the chat error bubble |
| Explicit LangGraph `AgentState` (payment_draft, missing_slots, active_txn_id, dialogue_intent) | **Deferred** — `create_agent()` (§2c) already runs on a LangGraph `StateGraph` internally, but a hand-rolled state machine with these exact fields wasn't built. Reason: the one capability it would add — letting the *same* agent draft and hold a pending payment across turns — conflicts with the deliberate safety boundary (§2c) that the chatbot can never create a transaction. Building it without crossing that boundary would mean a state machine that tracks a draft but still can't act on it, which doesn't earn its complexity. |
| Conversational interruption (pause a payment draft mid-flow, answer a question, resume) | **Partially done** (§8d) — correcting or cancelling a pending confirmation via natural follow-up text now works, without crossing the chatbot/payment safety boundary. Still deferred: a true resumable draft inside one LLM conversation state, for the same reason as the row above. |

## 8d. Correcting or cancelling a payment that's already awaiting confirmation — new

Reported bug: `"Pay 2 to Alice cooper"` opened a confirmation card; typing `"No actually alice wonderland"`
into the **main chat box** (not the card's own CONFIRM input) fell through to the support chatbot, which
answered an unrelated question about Alice Wonderland's balance — having no idea a payment was open on
screen. Root cause: the composer had no memory that a card was still pending, so every message was
interpreted in isolation as either a brand-new command or a chatbot question.

Fixed without touching the chatbot/payment safety boundary (§2c) — the chatbot still gains zero ability to
affect a transaction:

- New `POST /confirm/cancel` (`main.py`), mirroring the existing `_expire_confirmation()` helper —
  user-triggered instead of TTL-triggered. Only ever moves a *pending* confirmation to a terminal
  `"cancelled"` status (reusing the free-text `status` column, no schema change); cannot approve, create, or
  reopen anything.
- `frontend/app.js` tracks `state.activeDraft` — the most recently rendered still-open card (set in
  `renderConfirmationCard()`, cleared on confirm success) — and, only while one is open, checks a new
  composer message against simple deterministic regexes (zero LLM involvement) before the existing
  BPAY/`/command/text`/chatbot routing runs:
  - Explicit cancel words (`"cancel"`, `"nevermind"`, `"stop"`, ...) → cancel the draft via the new endpoint.
  - Correction language (a leading `"no"`, `"actually"`, `"instead"`, `"wait"`, `"I meant"`) → cancel the
    draft, then resubmit `"Pay <same amount> to <stripped target>"` through the *exact* same `/command/text`
    path everything else uses — the new target still goes through full parsing and the policy engine, so
    there's no step-up bypass.
  - A message ending in `"?"`, or one matching neither pattern (a bare name, an unrelated question, or a
    brand-new `"pay"` command with no correction wording) → completely unaffected, falls through exactly as
    before. This guards against the obvious false positive: `"I have no idea what you mean"` while a draft is
    open must not be read as a correction (the leading-`"no"` check is anchored to the *start* of the
    message specifically to avoid this).
- 12 scenarios (cancel-only, redirect-by-name, redirect-by-PayID, unrelated question, bare name, a genuine
  second payment command, a `?`-terminated message, an already-expired/approved confirmation, ...) were
  written out as a plan and simulated directly against the shipped regex/stripping logic before being
  considered done.

## 8e. Chatbot/payment-flow robustness — tester scenario sheet

Reported bug: `"pay alice"` failed to parse (no amount given), carried no `decision`, and the frontend's only
rule for that shape ("no decision → ask the chatbot") sent it to the local Ollama model, which took >120s and
was aborted. Prompted a full tester-style review of everything a chatbot like this one could plausibly face —
written out and verified against the actual code before anything was changed. Full sheet:

**A. Intent parsing / routing**

| # | Scenario | Verdict |
| --- | --- | --- |
| A1 | `"pay alice"` (verb + bare name, no amount) | **Fixed** — the reported bug. Answers instantly with "How much would you like to pay alice?", and `state.pendingSlotFill` (`frontend/app.js`) remembers the question so a bare follow-up reply like `"30"` completes it (synthesizes `"Pay 30 to alice"` through the normal `/command/text` path) instead of being sent to the chatbot with no context — this loop-completion was a follow-up fix after the first version only asked the question without listening for the answer |
| A2 | `"send 20"` (verb + bare amount, no recipient) | **Fixed** — same class as A1, same loop-completion (a bare name/PayID reply answers "who to?") |
| A2a | A bare verb alone, nothing else (`"pay"`) | **Fixed** — even less ambiguous than A1/A2 (nothing else in the message at all); answers instantly with "Who would you like to pay, and how much?", no chatbot call. Found live: Ollama was confirmed reachable at the time, so this was genuinely the 90s server-side timeout (§B1) firing on a message the chatbot could never have answered anyway |
| A3 | `"pay attention to this"` / `"send me my last transactions"` — idiomatic/informational text that happens to start with a payment verb | Confirmed these fail `intent_parser.py` with the *same* error text as A1/A2 — the fix had to be shape-based (exactly one trailing word), not error-text-based, specifically so these keep reaching the chatbot |
| A4 | Case/whitespace variance, self-correction, PayID-vs-name targets, no-preposition phrasing, explicit-PayID-qualified names | Already covered by existing parser tests |
| A5 | Non-English payment phrasing | Accepted limitation — the parser only recognizes English verbs |
| A6 | Very long chatbot input exceeding the local model's context window (`qwen2.5:7b-instruct` reports `context_length: 4096` tokens via Ollama) | Deferred — no truncation/warning yet, low probability for a chat UI |

**B. Chatbot reliability / resource safety**

| # | Scenario | Verdict |
| --- | --- | --- |
| B1 | Local model is slow/hangs | **Fixed** — `OLLAMA_TIMEOUT_SECONDS` (default 90s) bounds the call server-side |
| B2 | Ollama not running | Already handled — clear connection-error message |
| B3 | Wrong/unpulled model name | Already handled — clear 404 |
| B4 | Model too large for available RAM | Already handled — clear `std::bad_alloc`, no crash |
| B5 | Rapid resubmission while a chatbot reply is pending | **Fixed** — composer's text input is now disabled too, not just the send button |
| B6 | Several slow/hung chatbot calls exhausting FastAPI's shared sync thread pool | **Fixed** — bounded by the same B1 timeout |
| B7 | Model answers confidently but wrong despite the right tool being available (a 7B local model following tool-use instructions less reliably than a larger one) | Accepted limitation of self-hosting a small model — not fixable in prompt/code alone |
| B8 | `chat_messages` table grows unboundedly over time | Deferred — low impact; `_load_history()` already caps what's loaded per-prompt to the last 20 turns |

**C. Payment-flow / state** — all already handled in earlier work: ambiguous-contact CLARIFY picker (§3a),
correcting/cancelling a pending confirmation (§8d), PIN lockout/expiry/passkey, BPAY missing-slot prompts,
mid-sentence self-correction, duplicate-name same-vs-different-person resolution.

**D. Security / abuse**

| # | Scenario | Verdict |
| --- | --- | --- |
| D1 | SQL injection via free-text input | Already safe — every `*_repo.py` query uses parameterized `?` placeholders |
| D2 | XSS via a name/note/chatbot reply rendered into the page | Already safe — all dynamic HTML in `frontend/app.js` goes through `escapeHtml()` or `.textContent`, spot-checked every card-rendering function |
| D3 | Prompt injection trying to convince the chatbot it approved a payment | Already safe by architecture — the chatbot is never given a tool that can create/approve/execute a transaction, a hard boundary at the tool layer, not just a prompt instruction |
| D4 | No rate limiting on `/support/chat` or `/command/text` | Deferred — acceptable for a single-demo-user app |
| D5 | No size cap on `/voice/transcribe` audio uploads | Deferred — same reasoning as D4 |

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
support_chat.py          LangChain support chatbot (self-hosted Ollama only, §2d): read-only tools, per-user DB memory — see §2c
passkey_service.py       WebAuthn registration/authentication via py_webauthn — see §2e
passkey_repo.py          Passkey credential CRUD + sign-count tracking
voice_service.py         Local speech-to-text (faster-whisper) + spoken-number normalization — see §2f
bpay_parser.py           Free-text -> BPAY slots (biller/CRN/amount) parser — see §8a
bpay_repo.py             BPAY directory lookup, CRN Mod10 validation, saved billers
recurring_repo.py        Recurring/scheduled payment CRUD + due-schedule processing — see §8b
users_repo.py            Demo user bootstrap
payees_repo.py           Payee lookup/existence checks + get_payee() + find_payee_by_phone() + set_payee_connected_account()
payid_directory_repo.py  200-entry mock external PayID registry: seed_payid_directory(), lookup_payid(), looks_like_payid()
payment_methods_repo.py  Default payment method get/set
transactions_repo.py     Transaction CRUD + set_receiver_evidence() + create_pending_bpay_transaction()
confirmations_repo.py    Confirmation CRUD + expiry check + set_confirmation_challenge() (WebAuthn)
stripe_service.py        Stripe init, PaymentIntent creation (idempotency + destination charges), test
                         payment method seeding, test Connect account creation, receiver balance lookup
check_db.py              Dev script: list DB tables
check_pm.py              Dev script: dump payment_methods
create_stripe_pm.py      Dev script: seed a Stripe test card/customer
VocalPay_System_Blueprint.docx  The architectural blueprint document this round of work was built from
requirements.txt         Pinned Python dependencies (langchain/langgraph/libsql-client/webauthn/faster-whisper/python-docx)
vocalpay.db              Local SQLite database file (dev only — prod uses Turso, see §2b)
.env                     STRIPE_SECRET_KEY / TURSO_* / OLLAMA_* / WEBAUTHN_* / etc. (local secrets, keep out of git)
tests/conftest.py        Isolated-DB pytest fixture
tests/test_vocalpay.py   73 integration tests (payments, contacts, PayID, support chat, BPAY, passkeys, recurring)
frontend/index.html      Chat UI shell + Setup/Audit tabbed side panel (Tailwind CDN), now with Passkeys + BPAY billers
frontend/app.js          All frontend logic: session, chat, confirm cards + PayID + BPAY, pay execution + receiver
                         evidence, audit panel, setup tab (contacts, balance checks, passkeys, billers), voice,
                         WebAuthn base64url<->ArrayBuffer helpers
frontend/styles.css      Chat bubble/PIN-box/waveform/spinner styling
```
