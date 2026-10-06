-- Users of the system
CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  pin_hash TEXT,
  created_at TEXT NOT NULL
);

-- Saved payees (e.g., "John" -> PayID/email/mobile or Stripe recipient mapping later)
CREATE TABLE IF NOT EXISTS payees (
  payee_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  nickname TEXT NOT NULL,
  type TEXT NOT NULL,           -- "payid" or "stripe" (for now)
  identifier TEXT NOT NULL,     -- e.g., payid value/email/mobile OR any reference string
  phone_number TEXT,                    -- contact's mobile number, shown as the PayID
  stripe_connected_account_id TEXT,     -- Stripe Connect account that actually receives funds
  is_contact INTEGER NOT NULL DEFAULT 1, -- 1 = deliberately saved contact; 0 = auto-provisioned from a
                                         -- direct PayID payment, not yet saved (still fully payable/trackable)
  linked_contact_id TEXT,               -- set when the user has confirmed this row is "the same person" as
                                         -- another payee (e.g. a home/work number) — points at that payee_id
  created_at TEXT NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- Tokenized payment methods (NEVER store card numbers)
CREATE TABLE IF NOT EXISTS payment_methods (
  pm_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  label TEXT NOT NULL,                  -- e.g., "Visa (demo)"
  stripe_customer_id TEXT,
  stripe_payment_method_id TEXT,
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- Transactions (logical record)
CREATE TABLE IF NOT EXISTS transactions (
  txn_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL,
  payee_id TEXT,
  status TEXT NOT NULL,                 -- "created" | "succeeded" | "failed"
  stripe_payment_intent_id TEXT,
  stripe_transfer_id TEXT,              -- the Transfer that moved funds to the payee's connected account
  destination_account_id TEXT,          -- payee's Stripe Connect account id, snapshotted at execute time
  receiver_confirmed_at TEXT,           -- set once we've verified the destination account actually has the funds
  rail TEXT NOT NULL DEFAULT 'payid',   -- "payid" (card -> Stripe/Connect) | "bpay" (simulated settlement)
  bpay_biller_code TEXT,
  bpay_crn TEXT,
  service_invoice_id TEXT,              -- set when rail='service': the invoice being paid
  created_at TEXT NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(user_id),
  FOREIGN KEY (payee_id) REFERENCES payees(payee_id)
);

-- Audit events (append-only log)
CREATE TABLE IF NOT EXISTS audit_events (
  event_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  prev_hash TEXT,
  event_hash TEXT NOT NULL
);

-- Helpful indexes (speed)
CREATE INDEX IF NOT EXISTS idx_payees_user_nickname ON payees(user_id, nickname);
CREATE INDEX IF NOT EXISTS idx_audit_session_ts ON audit_events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_txn_user_created ON transactions(user_id, created_at);

-- Pending confirmations for transactions (PIN-based)
CREATE TABLE IF NOT EXISTS confirmations (
  confirmation_id TEXT PRIMARY KEY,
  txn_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  required_confirmation TEXT NOT NULL,   -- "normal" | "pin" | "passkey"
  pin_attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  challenge TEXT,                       -- WebAuthn challenge, only set when required_confirmation="passkey"
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,                 -- "pending" | "approved" | "rejected" | "expired"
  FOREIGN KEY (txn_id) REFERENCES transactions(txn_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_confirmations_txn ON confirmations(txn_id);

-- External PayID registry (simulates a bank-network-style directory): any
-- number here is a "valid" PayID that resolves to a registered name, whether
-- or not the current user has saved them as a contact yet.
CREATE TABLE IF NOT EXISTS payid_directory (
  phone_number TEXT PRIMARY KEY,   -- normalized, digits-only
  display_number TEXT NOT NULL,    -- formatted for display, e.g. "0400 111 222"
  registered_name TEXT NOT NULL
);

-- FIDO2/WebAuthn passkey credentials — an additional step-up confirmation
-- method alongside the PIN (never a replacement): registered via
-- POST /webauthn/register, asserted via POST /confirm/passkey.
CREATE TABLE IF NOT EXISTS passkey_credentials (
  credential_id TEXT PRIMARY KEY,       -- base64url credential id from the authenticator
  user_id TEXT NOT NULL,
  public_key_cbor BYTEA NOT NULL,       -- COSE public key, as returned by py_webauthn
  sign_count INTEGER NOT NULL DEFAULT 0, -- cloned-authenticator detection: must only increase
  transports_json TEXT,                 -- e.g. '["internal","hybrid"]'
  label TEXT,                           -- user-facing name, e.g. "MacBook Touch ID"
  created_at TEXT NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_passkey_credentials_user ON passkey_credentials(user_id);

-- Australian BPAY biller directory (demo/mock — BPAY has no Stripe test-mode
-- equivalent, so this rail is simulated: validated and audited exactly like
-- every other payment, but settlement is recorded, not sent over a real
-- BPAY network). crn_rule names which check digit algorithm applies to that
-- biller's Customer Reference Number — "MOD10" (Luhn-style) is the common one.
CREATE TABLE IF NOT EXISTS bpay_directory (
  biller_code TEXT PRIMARY KEY,
  biller_name TEXT NOT NULL,
  crn_rule TEXT NOT NULL DEFAULT 'MOD10',
  min_amount_cents INTEGER NOT NULL DEFAULT 100,
  max_amount_cents INTEGER NOT NULL DEFAULT 10000000
);

-- A user's saved billers (nickname -> biller code + their specific CRN, e.g.
-- an account number), the BPAY equivalent of a saved PayID contact.
CREATE TABLE IF NOT EXISTS saved_bpay_billers (
  biller_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  nickname TEXT NOT NULL,
  biller_code TEXT NOT NULL,
  crn TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(user_id),
  FOREIGN KEY (biller_code) REFERENCES bpay_directory(biller_code),
  UNIQUE (user_id, nickname)
);

-- Recurring/scheduled payments. next_run_at is advanced by
-- recurring_repo.process_due_schedules() — a function callable on demand or
-- from an external cron trigger; this app has no built-in cron daemon.
CREATE TABLE IF NOT EXISTS recurring_schedules (
  schedule_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  payment_rail TEXT NOT NULL,        -- "payid" | "bpay"
  target_identifier TEXT NOT NULL,   -- payee_id (payid rail) or biller_id (bpay rail)
  amount_cents INTEGER NOT NULL,
  cadence TEXT NOT NULL,             -- "weekly" | "fortnightly" | "monthly"
  next_run_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', -- "active" | "paused" | "cancelled"
  created_at TEXT NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_recurring_schedules_due ON recurring_schedules(status, next_run_at);

-- Service providers (mock utilities, telco, health, transport) and the open
-- invoices a user owes them. Settlement is simulated, like BPAY.
CREATE TABLE IF NOT EXISTS service_providers (
  provider_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,               -- "utility" | "telco" | "health" | "transport"
  accent TEXT NOT NULL                  -- colour key for the UI rail, e.g. "indigo"
);

CREATE TABLE IF NOT EXISTS service_invoices (
  invoice_id TEXT PRIMARY KEY,
  provider_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  customer_ref TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'AUD',
  due_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',  -- "open" | "paid"
  paid_txn_id TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (provider_id) REFERENCES service_providers(provider_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);
