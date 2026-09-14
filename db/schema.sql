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
  required_confirmation TEXT NOT NULL,   -- "normal" or "pin"
  pin_attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,                 -- "pending" | "approved" | "rejected" | "expired"
  FOREIGN KEY (txn_id) REFERENCES transactions(txn_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_confirmations_txn ON confirmations(txn_id);

-- Support chatbot conversation history — per user (not per session), so the
-- assistant remembers past conversations across visits. role is "human" or
-- "ai"; tool-call/tool-result turns aren't persisted, only the final
-- human-readable exchange.
CREATE TABLE IF NOT EXISTS chat_messages (
  message_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_user_created ON chat_messages(user_id, created_at);

-- External PayID registry (simulates a bank-network-style directory): any
-- number here is a "valid" PayID that resolves to a registered name, whether
-- or not the current user has saved them as a contact yet.
CREATE TABLE IF NOT EXISTS payid_directory (
  phone_number TEXT PRIMARY KEY,   -- normalized, digits-only
  display_number TEXT NOT NULL,    -- formatted for display, e.g. "0400 111 222"
  registered_name TEXT NOT NULL
);
