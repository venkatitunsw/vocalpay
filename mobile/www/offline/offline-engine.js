// Offline port of main.py + its repo modules. Every endpoint the frontend
// calls (see the `api()` wrapper in app.js) is reproduced here against
// IndexedDB (idb.js) instead of Postgres, with Stripe and the BPAY/service
// networks replaced by clearly-labelled mocks — see MOBILE_OFFLINE.md in
// this folder for exactly what that means and what's NOT ported (passkeys,
// recurring payments, the LLM chatbot, service-provider bills).
(function () {
  const DB = window.VPDB;
  const DEMO_USER_ID = "demo-user";
  const DEMO_USER_PIN = "1234";
  const HARD_CAP_AUD = 50.0;

  const uuid = () =>
    "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  const nowIso = () => new Date().toISOString();

  // --- audit chain (port of audit.py) ---------------------------------------
  function canonicalJson(data) {
    const sortKeys = (v) => {
      if (Array.isArray(v)) return v.map(sortKeys);
      if (v && typeof v === "object") {
        return Object.keys(v)
          .sort()
          .reduce((acc, k) => ((acc[k] = sortKeys(v[k])), acc), {});
      }
      return v;
    };
    return JSON.stringify(sortKeys(data));
  }
  async function sha256Hex(text) {
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  async function getLastEventHash(sessionId) {
    const events = await DB.getAllByIndex("audit_events", "session_id", sessionId);
    if (!events.length) return null;
    events.sort((a, b) => (a.ts < b.ts ? -1 : 1));
    return events[events.length - 1].event_hash;
  }
  async function appendEvent(sessionId, eventType, payload) {
    const event_id = uuid();
    const ts = nowIso();
    const prev_hash = await getLastEventHash(sessionId);
    const chainInput = canonicalJson({ event_id, session_id: sessionId, ts, event_type: eventType, payload, prev_hash });
    const event_hash = await sha256Hex(chainInput);
    const event = { event_id, session_id: sessionId, ts, event_type: eventType, payload, prev_hash, event_hash };
    await DB.put("audit_events", event);
    return event;
  }
  async function verifySessionChain(sessionId) {
    const events = (await DB.getAllByIndex("audit_events", "session_id", sessionId)).sort((a, b) => (a.ts < b.ts ? -1 : 1));
    if (!events.length) return { ok: true, session_id: sessionId, events: 0, message: "No events" };
    let prev = null;
    for (let i = 0; i < events.length; i++) {
      const ev = events[i];
      const expectedPrev = prev ? prev.event_hash : null;
      if (ev.prev_hash !== expectedPrev) {
        return { ok: false, session_id: sessionId, events: events.length, broken_at_index: i, reason: "prev_hash mismatch" };
      }
      const computed = await sha256Hex(
        canonicalJson({ event_id: ev.event_id, session_id: ev.session_id, ts: ev.ts, event_type: ev.event_type, payload: ev.payload, prev_hash: ev.prev_hash })
      );
      if (computed !== ev.event_hash) {
        return { ok: false, session_id: sessionId, events: events.length, broken_at_index: i, reason: "event_hash mismatch" };
      }
      prev = ev;
    }
    return { ok: true, session_id: sessionId, events: events.length };
  }

  // --- PayID validation (port of payid_validation.py) -----------------------
  const EMAIL_RE = /^[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.[a-z]{2,}$/;
  const ABN_WEIGHTS = [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19];
  class PayIDValidationError extends Error {}
  const digitsOnly = (raw) => (raw || "").replace(/[\s\-()]/g, "");
  function normalizeMobile(raw) {
    let compact = digitsOnly(raw);
    if (compact.startsWith("+")) compact = compact.slice(1);
    if (/^61\d+$/.test(compact) && compact.length === 11) compact = "0" + compact.slice(2);
    if (!/^\d+$/.test(compact)) throw new PayIDValidationError("A mobile PayID can only contain digits, spaces, or a leading +61.");
    if (compact.length !== 10) throw new PayIDValidationError(`That looks like a phone number with ${compact.length} digits. An Australian mobile has 10.`);
    if (!compact.startsWith("04")) throw new PayIDValidationError("An Australian mobile PayID starts with 04 (or +61 4).");
    return compact;
  }
  function normalizeEmail(raw) {
    const value = (raw || "").trim().toLowerCase();
    if (value.includes(" ") || (value.match(/@/g) || []).length !== 1) throw new PayIDValidationError("An email PayID needs exactly one @ and no spaces.");
    if (!EMAIL_RE.test(value)) throw new PayIDValidationError("That email address doesn't look valid (check the domain, e.g. name@example.com).");
    return value;
  }
  function validateAbn(raw) {
    const digits = digitsOnly(raw);
    if (!/^\d{11}$/.test(digits)) throw new PayIDValidationError("An ABN PayID must be exactly 11 digits.");
    const adjusted = [parseInt(digits[0], 10) - 1, ...digits.slice(1).split("").map(Number)];
    const sum = adjusted.reduce((acc, d, i) => acc + ABN_WEIGHTS[i] * d, 0);
    if (sum % 89 !== 0) throw new PayIDValidationError("That ABN fails the check-digit test, so it isn't a valid ABN.");
    return digits;
  }
  function classifyPayid(raw) {
    const text = (raw || "").trim();
    if (!text) throw new PayIDValidationError("Enter a mobile number, email address, or ABN.");
    if (text.includes("@")) return ["email", normalizeEmail(text)];
    const compact = digitsOnly(text);
    if (/^\d{11}$/.test(compact) && !compact.startsWith("04") && !compact.startsWith("61")) return ["abn", validateAbn(compact)];
    return ["mobile", normalizeMobile(text)];
  }

  // --- PayID directory (port of payid_directory_repo.py) --------------------
  const normalizePayid = (raw) => (raw || "").replace(/\D/g, "");
  const looksLikePayid = (raw) => {
    const d = normalizePayid(raw);
    return d.length >= 8 && d.length <= 10;
  };
  const FIRST_NAMES = ["Olivia","Liam","Emma","Noah","Amelia","Jack","Charlotte","William","Mia","James","Isla","Benjamin","Ava","Lucas","Grace","Henry","Chloe","Oliver","Zoe","Ethan","Ruby","Alexander","Lily","Mason","Ella","Samuel","Sophia","Thomas","Harper","Daniel"];
  const LAST_NAMES = ["Smith","Jones","Williams","Brown","Wilson","Taylor","Nguyen","Anderson","Lee","Martin","Clarke","Walker","Harris","Young","King","Wright","Hill","Baker","Chen","Patel"];
  function formatDisplay(digits) {
    return digits.length === 10 ? `${digits.slice(0, 4)} ${digits.slice(4, 7)} ${digits.slice(7, 10)}` : digits;
  }
  async function seedPayidDirectory(count = 200) {
    if ((await DB.count("payid_directory")) >= count) return;
    const rows = [];
    const seen = new Set();
    let i = 0;
    while (rows.length < count) {
      i += 1;
      const digits = `04${String((17 * i + 23) % 100).padStart(2, "0")}${String((41 * i + 7) % 1000).padStart(3, "0")}${String((97 * i + 511) % 1000).padStart(3, "0")}`;
      if (seen.has(digits)) continue;
      seen.add(digits);
      const first = FIRST_NAMES[i % FIRST_NAMES.length];
      const last = LAST_NAMES[(i * 3) % LAST_NAMES.length];
      rows.push({
        phone_number: digits,
        display_number: formatDisplay(digits),
        registered_name: `${first} ${last}`,
        email: `${first.toLowerCase()}.${last.toLowerCase()}${i}@example.com`,
      });
    }
    await DB.putMany("payid_directory", rows);
  }
  async function lookupPayid(rawNumber) {
    const digits = normalizePayid(rawNumber);
    if (!digits) return null;
    return await DB.get("payid_directory", digits);
  }
  async function lookupPayidEmail(rawEmail) {
    const email = (rawEmail || "").trim().toLowerCase();
    if (!email) return null;
    const matches = await DB.getAllByIndex("payid_directory", "email", email);
    return matches[0] || null;
  }

  // --- BPAY directory (port of bpay_repo.py) ---------------------------------
  const DEMO_BILLERS = [
    ["111999", "Origin Energy"],
    ["222888", "Telstra"],
    ["333777", "City of Melbourne Rates"],
    ["444666", "AGL Energy"],
  ];
  async function seedBpayDirectory() {
    if ((await DB.count("bpay_directory")) >= DEMO_BILLERS.length) return;
    await DB.putMany(
      "bpay_directory",
      DEMO_BILLERS.map(([code, name]) => ({ biller_code: code, biller_name: name, crn_rule: "MOD10", min_amount_cents: 100, max_amount_cents: 10000000 }))
    );
  }
  function normalizeCrn(raw) {
    return (raw || "").replace(/\D/g, "");
  }
  function validateCrnMod10(crn) {
    const digits = normalizeCrn(crn);
    if (digits.length < 2) return false;
    let total = 0;
    const rev = digits.split("").reverse();
    for (let i = 0; i < rev.length; i++) {
      let d = parseInt(rev[i], 10);
      if (i === 0) {
        total += d;
        continue;
      }
      if (i % 2 === 1) {
        d *= 2;
        if (d > 9) d -= 9;
      }
      total += d;
    }
    return total % 10 === 0;
  }
  async function lookupBiller(billerCode) {
    return await DB.get("bpay_directory", (billerCode || "").trim());
  }
  async function findBillerByName(name) {
    if (!name) return null;
    const rows = await DB.getAll("bpay_directory");
    const nameNorm = name.trim().toLowerCase();
    const exact = rows.find((r) => r.biller_name.toLowerCase() === nameNorm);
    if (exact) return exact;
    return rows.find((r) => r.biller_name.toLowerCase().includes(nameNorm) || r.biller_name.toLowerCase().split(" ")[0] === nameNorm.split(" ")[0]) || null;
  }
  async function findSavedBillerByNickname(userId, nickname) {
    const rows = await DB.getAllByIndex("saved_bpay_billers", "user_id", userId);
    return rows.find((r) => r.nickname.toLowerCase() === nickname.trim().toLowerCase()) || null;
  }
  async function listSavedBillers(userId) {
    return (await DB.getAllByIndex("saved_bpay_billers", "user_id", userId)).sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
  }

  // --- payees (port of payees_repo.py / payee_payids_repo.py) ----------------
  async function getPayee(payeeId) {
    return await DB.get("payees", payeeId);
  }
  async function findPayeeByPhone(userId, rawNumber) {
    const digits = normalizePayid(rawNumber);
    if (!digits) return null;
    const rows = await DB.getAllByIndex("payees", "user_id", userId);
    return rows.find((r) => r.phone_number && normalizePayid(r.phone_number) === digits) || null;
  }
  async function findPayeeByPayidValue(userId, value) {
    const matches = await DB.getAllByIndex("payee_payids", "value_normalized", value);
    const mine = matches.find((pp) => pp.user_id === userId);
    return mine ? await getPayee(mine.payee_id) : null;
  }
  async function payeeExistsForUser(userId, nickname) {
    const rows = await DB.getAllByIndex("payees", "user_id", userId);
    return rows.some((r) => r.nickname.toLowerCase() === nickname.toLowerCase());
  }
  async function findSavedContactByNickname(userId, nickname) {
    const rows = await DB.getAllByIndex("payees", "user_id", userId);
    return rows.find((r) => r.is_contact && r.nickname.toLowerCase() === nickname.trim().toLowerCase()) || null;
  }
  function groupAnchorId(payee) {
    return payee.linked_contact_id || payee.payee_id;
  }
  function resolveNameMatches(matches) {
    if (matches.length <= 1) return matches;
    const anchors = new Set(matches.map(groupAnchorId));
    if (anchors.size !== 1) return matches;
    const saved = matches.filter((m) => m.is_contact);
    if (saved.length === 1) return [saved[0]];
    const anchorRows = matches.filter((m) => !m.linked_contact_id);
    return anchorRows.length ? [anchorRows[0]] : [matches[0]];
  }
  async function findPayeesByName(userId, name) {
    const nameNorm = name.trim().toLowerCase();
    const rows = await DB.getAllByIndex("payees", "user_id", userId);
    const exact = rows.filter((r) => r.nickname.toLowerCase() === nameNorm);
    if (exact.length) return resolveNameMatches(exact);
    const firstWord = rows.filter((r) => r.nickname.trim().toLowerCase().split(" ")[0] === nameNorm);
    if (firstWord.length) return resolveNameMatches(firstWord);
    return resolveNameMatches(rows.filter((r) => r.nickname.toLowerCase().includes(nameNorm)));
  }
  async function listPayids(payeeId) {
    return (await DB.getAllByIndex("payee_payids", "payee_id", payeeId)).sort((a, b) => (b.is_primary - a.is_primary) || (a.created_at < b.created_at ? -1 : 1));
  }
  async function addPayid(userId, payeeId, raw, label) {
    let type, value;
    try {
      [type, value] = classifyPayid(raw);
    } catch (e) {
      return { ok: false, error: e.message };
    }
    const owners = await DB.getAllByIndex("payee_payids", "value_normalized", value);
    if (owners.length) {
      const owner = owners[0];
      if (owner.payee_id === payeeId) return { ok: false, error: `${value} is already saved to this contact.` };
      const ownerPayee = await getPayee(owner.payee_id);
      return { ok: false, error: `${value} already belongs to ${ownerPayee.nickname}. A PayID can only be saved to one contact.`, conflict: { payee_id: owner.payee_id, nickname: ownerPayee.nickname } };
    }
    const existingCount = (await DB.getAllByIndex("payee_payids", "payee_id", payeeId)).length;
    const payid_id = uuid();
    await DB.put("payee_payids", { payid_id, payee_id: payeeId, user_id: userId, pay_id_type: type, value_normalized: value, label: label || null, is_primary: existingCount === 0 ? 1 : 0, created_at: nowIso() });
    return { ok: true, payid_id, pay_id_type: type, value };
  }

  // --- mock Stripe (clearly fake; no network call anywhere) ------------------
  function mockConnectedAccount() {
    return "acct_offline_" + uuid().replace(/-/g, "").slice(0, 16);
  }
  function mockPaymentIntent() {
    return { id: "pi_offline_" + uuid().replace(/-/g, "").slice(0, 16), status: "succeeded", latest_charge: "ch_offline_" + uuid().slice(0, 8) };
  }
  const mockBalances = {}; // destination_account_id -> cents, so repeated balance checks look consistent
  function mockBalanceFor(accountId) {
    if (!(accountId in mockBalances)) mockBalances[accountId] = 0;
    return { available_cents: mockBalances[accountId], pending_cents: 0, currency: "aud" };
  }

  // --- policy (port of policy.py) --------------------------------------------
  function decideNext(intent, payeeExists, payeeHasReceiverTracking, hardCapAud = HARD_CAP_AUD) {
    if (intent.amount <= 0) return { decision: "BLOCK", reason: "Amount must be > 0", required_confirmation: "none", risk_level: "high" };
    if (!payeeExists) {
      return { decision: "BLOCK", reason: `"${intent.payee_name}" is not a saved contact. Add them as a contact with a PayID in Setup before paying.`, required_confirmation: "none", risk_level: "high" };
    }
    if (!payeeHasReceiverTracking) {
      return { decision: "BLOCK", reason: `${intent.payee_name} has no PayID on file. Add them as a contact with a phone number/PayID in Setup before paying.`, required_confirmation: "none", risk_level: "high" };
    }
    if (intent.amount > hardCapAud) return { decision: "STEP_UP", reason: `Amount exceeds hard cap ${hardCapAud} AUD`, required_confirmation: "pin", risk_level: "high" };
    return { decision: "PROCEED", reason: "Within cap and payee known", required_confirmation: "normal", risk_level: "low" };
  }

  // --- intent parser (port of intent_parser.py) -------------------------------
  const VERB_RE = /^\s*(pay|send|transfer)\b/i;
  const AMOUNT_RE = /\d+(?:\.\d+)?/g;
  const TARGET_CHARS = String.raw`[a-zA-Z0-9\s.\-':()@]`;
  const TARGET_STOP = String.raw`(?=\s+for\b|,|\.+(?:\s|$)|$)`;
  const TO_TARGET_RE = new RegExp(String.raw`\bto\s+(?<target>[a-zA-Z0-9]${TARGET_CHARS}*?)${TARGET_STOP}`, "i");
  const ON_ACCOUNT_RE = new RegExp(String.raw`\bon\s+(?<target>[a-zA-Z0-9]${TARGET_CHARS}*?)(?:'s)?\s+account\b`, "i");
  const FOR_TARGET_RE = new RegExp(String.raw`\bfor\s+(?<target>[a-zA-Z0-9]${TARGET_CHARS}*?)(?=\s+for\b|\s+\d|[,.]|$)`, "i");
  const NOTE_RE = /\bfor\s+(?<note>.+)$/i;
  const TRAILING_ACCOUNT_RE = /(?:'s)?\s+account\s*$/i;
  const EXPLICIT_PAYID_RE = /(?:\bpayid\b\s*[:\-]?\s*|\()(?<number>\d[\d\s\-]{6,13}\d)\)?/i;

  function parseTextCommand(text) {
    if (!text || text.trim().length < 3) return { ok: false, error: "Empty or too short command" };
    const raw = text.trim();
    if (!VERB_RE.test(raw)) return { ok: false, error: "Could not parse command. Try: 'Pay 12 to John'" };

    let target = null;
    let targetSpan = null;
    for (const pattern of [TO_TARGET_RE, ON_ACCOUNT_RE]) {
      const m = pattern.exec(raw);
      if (m) {
        target = m.groups.target.trim();
        targetSpan = [m.index, m.index + m[0].length];
        break;
      }
    }
    if (target === null) {
      const m = FOR_TARGET_RE.exec(raw);
      if (m) {
        target = m.groups.target.trim();
        targetSpan = [m.index, m.index + m[0].length];
      }
    }
    if (target === null) {
      const verbM = VERB_RE.exec(raw);
      const afterVerb = raw.slice(verbM[0].length);
      const digitM = /\d/.exec(afterVerb);
      if (digitM && digitM.index > 0) {
        const candidate = afterVerb.slice(0, digitM.index).trim().replace(/^[ ,$]+|[ ,$]+$/g, "");
        if (/^[a-zA-Z]/.test(candidate)) {
          target = candidate;
          targetSpan = [verbM[0].length, verbM[0].length + digitM.index];
        }
      }
    }
    if (!target) return { ok: false, error: "Could not figure out who to pay. Try: 'Pay 12 to John'" };

    const payidM = EXPLICIT_PAYID_RE.exec(target);
    if (payidM) {
      target = payidM.groups.number;
    } else {
      target = target.replace(TRAILING_ACCOUNT_RE, "").trim();
    }
    if (!target) return { ok: false, error: "Could not figure out who to pay. Try: 'Pay 12 to John'" };

    const afterTarget = raw.slice(targetSpan[1]);
    let note = "";
    const noteM = NOTE_RE.exec(afterTarget);
    if (noteM) note = noteM.groups.note.trim().replace(/\.$/, "");

    const beforeTarget = raw.slice(0, targetSpan[0]);
    const correctionRegion = noteM ? afterTarget.slice(0, noteM.index) : afterTarget;
    const amounts = [...(beforeTarget + " " + correctionRegion).matchAll(AMOUNT_RE)].map((m) => parseFloat(m[0]));
    if (!amounts.length) return { ok: false, error: "Could not find an amount to pay. Try: 'Pay 12 to John'" };

    const amount = amounts[amounts.length - 1];
    return { ok: true, intent: { action: "pay", amount, currency: "AUD", payee_name: target, note } };
  }

  // --- BPAY parser (port of bpay_parser.py) -----------------------------------
  const BPAY_KEYWORD_RE = /\bbpay\b|\bbiller\s*code\b/i;
  const BILLER_CODE_RE = /biller\s*code\s*[:\-]?\s*(?<code>\d+)/i;
  const CRN_RE = /(?:reference|crn|ref)\s*[:\-]?\s*(?<crn>\d[\d\s]*\d|\d)/i;
  const BILLER_NAME_RE = /\b(?:pay|send|transfer)\s+(?<name>[a-zA-Z][a-zA-Z\s]*?)\s+bpay\b/i;
  function isBpayCommand(text) {
    return BPAY_KEYWORD_RE.test(text || "");
  }
  function parseBpayCommand(text) {
    if (!isBpayCommand(text)) return { ok: false, error: "Not a BPAY command" };
    const billerCodeM = BILLER_CODE_RE.exec(text);
    const crnM = CRN_RE.exec(text);
    const nameM = BILLER_NAME_RE.exec(text);
    let scrubbed = text.replace(new RegExp(BILLER_CODE_RE.source, "gi"), " ").replace(new RegExp(CRN_RE.source, "gi"), " ");
    const amounts = [...scrubbed.matchAll(AMOUNT_RE)].map((m) => parseFloat(m[0]));
    const biller_name = nameM ? nameM.groups.name.trim() : null;
    const biller_code = billerCodeM ? billerCodeM.groups.code : null;
    const crn = crnM ? crnM.groups.crn.replace(/\s+/g, "") : null;
    const amount = amounts.length ? amounts[amounts.length - 1] : null;
    const missing = [];
    if (!biller_code && !biller_name) missing.push("biller");
    if (!crn) missing.push("crn");
    if (amount === null) missing.push("amount");
    return { ok: true, biller_name, biller_code, crn, amount, missing };
  }

  // --- transactions / confirmations (port of transactions_repo.py / confirmations_repo.py) ---
  async function createPendingTransaction(sessionId, userId, amountCents, currency, payeeId) {
    const txn_id = uuid();
    await DB.put("transactions", { txn_id, session_id: sessionId, user_id: userId, amount_cents: amountCents, currency, payee_id: payeeId, status: "created", stripe_payment_intent_id: null, rail: "payid", created_at: nowIso() });
    return txn_id;
  }
  async function createPendingBpayTransaction(sessionId, userId, amountCents, currency, billerCode, crn) {
    const txn_id = uuid();
    await DB.put("transactions", { txn_id, session_id: sessionId, user_id: userId, amount_cents: amountCents, currency, payee_id: null, status: "created", stripe_payment_intent_id: null, rail: "bpay", bpay_biller_code: billerCode, bpay_crn: crn, created_at: nowIso() });
    return txn_id;
  }
  async function updateTransactionStatus(txnId, status) {
    const txn = await DB.get("transactions", txnId);
    if (txn) {
      txn.status = status;
      await DB.put("transactions", txn);
    }
  }
  async function getTransaction(txnId) {
    return await DB.get("transactions", txnId);
  }
  async function listRecentTransactions(userId, limit = 5) {
    const rows = (await DB.getAllByIndex("transactions", "user_id", userId)).sort((a, b) => (a.created_at < b.created_at ? 1 : -1)).slice(0, limit);
    const out = [];
    for (const t of rows) {
      const payee = t.payee_id ? await getPayee(t.payee_id) : null;
      out.push({ ...t, payee_nickname: payee ? payee.nickname : null });
    }
    return out;
  }
  function isConfirmationExpired(conf) {
    return new Date() >= new Date(conf.expires_at);
  }
  async function createConfirmation(txnId, userId, requiredConfirmation, ttlSeconds = 120) {
    const confirmation_id = uuid();
    const now = new Date();
    const expires_at = new Date(now.getTime() + ttlSeconds * 1000).toISOString();
    const conf = { confirmation_id, txn_id: txnId, user_id: userId, required_confirmation: requiredConfirmation, pin_attempts: 0, max_attempts: 3, expires_at, created_at: now.toISOString(), status: "pending" };
    await DB.put("confirmations", conf);
    return conf;
  }
  async function getConfirmation(confirmationId) {
    return await DB.get("confirmations", confirmationId);
  }
  async function incrementAttempts(confirmationId) {
    const conf = await DB.get("confirmations", confirmationId);
    conf.pin_attempts += 1;
    await DB.put("confirmations", conf);
    return conf.pin_attempts;
  }
  async function setConfirmationStatus(confirmationId, status) {
    const conf = await DB.get("confirmations", confirmationId);
    if (conf) {
      conf.status = status;
      await DB.put("confirmations", conf);
    }
  }

  // --- startup seeding ---------------------------------------------------------
  async function ensureDemoUser() {
    const existing = await DB.get("users", DEMO_USER_ID);
    if (existing) return;
    await DB.put("users", { user_id: DEMO_USER_ID, name: "Demo User", pin_hash: DEMO_USER_PIN, created_at: nowIso() });
  }
  let _ready = null;
  function ready() {
    if (!_ready) {
      _ready = (async () => {
        await ensureDemoUser();
        await seedPayidDirectory(200);
        await seedBpayDirectory();
      })();
    }
    return _ready;
  }

  // --- shared response helpers (mirrors main.py's _blocked_response etc.) ----
  async function blockedResponse(sessionId, intent, reason) {
    const decision = { decision: "BLOCK", reason, required_confirmation: "none", risk_level: "high" };
    await appendEvent(sessionId, "DECISION", decision);
    return { ok: false, intent, payee_exists: false, decision };
  }
  async function provisionDirectoryPayee(sessionId, intent, directoryMatch, payidValues) {
    const connected_account_id = mockConnectedAccount();
    const payee_id = uuid();
    const now = nowIso();
    await DB.put("payees", {
      payee_id, user_id: DEMO_USER_ID, nickname: directoryMatch.registered_name, type: "stripe",
      identifier: directoryMatch.display_number, phone_number: directoryMatch.display_number,
      stripe_connected_account_id: connected_account_id, is_contact: 0, linked_contact_id: null, created_at: now,
    });
    for (const value of payidValues) {
      if (value) await addPayid(DEMO_USER_ID, payee_id, value);
    }
    const payee = await getPayee(payee_id);
    await appendEvent(sessionId, "PAYID_AUTO_PROVISIONED", {
      payee_id, phone_number: directoryMatch.display_number, registered_name: directoryMatch.registered_name, stripe_connected_account_id: connected_account_id,
    });
    return payee;
  }

  // --- /command/text (port of main.py command_text) ---------------------------
  async function commandText(sessionId, text) {
    await appendEvent(sessionId, "COMMAND_TEXT_RECEIVED", { text });
    const result = parseTextCommand(text);
    if (!result.ok) {
      await appendEvent(sessionId, "INTENT_PARSE_FAILED", { error: result.error });
      return { ok: false, error: result.error };
    }
    const intent = result.intent;
    await appendEvent(sessionId, "INTENT_PARSED", intent);

    let payeeRecord = null;
    let exists = false;

    if (intent.payee_name.includes("@")) {
      const payidRaw = intent.payee_name.trim().toLowerCase();
      payeeRecord = await findPayeeByPayidValue(DEMO_USER_ID, payidRaw);
      exists = !!payeeRecord;
      if (!payeeRecord) {
        const directoryMatch = await lookupPayidEmail(payidRaw);
        await appendEvent(sessionId, "PAYEE_LOOKUP", { payee_name: payidRaw, exists: false, by: "email", directory_match: directoryMatch });
        if (!directoryMatch) return await blockedResponse(sessionId, intent, `"${payidRaw}" is not a registered PayID.`);
        payeeRecord = await provisionDirectoryPayee(sessionId, intent, directoryMatch, [directoryMatch.email, directoryMatch.display_number]);
        exists = true;
      }
      intent.payee_name = payeeRecord.nickname;
      await appendEvent(sessionId, "PAYEE_LOOKUP", { payee_name: intent.payee_name, exists: true, by: "email" });
    } else if (looksLikePayid(intent.payee_name)) {
      const payidRaw = intent.payee_name;
      payeeRecord = await findPayeeByPhone(DEMO_USER_ID, payidRaw);
      exists = !!payeeRecord;
      if (!payeeRecord) {
        const directoryMatch = await lookupPayid(payidRaw);
        await appendEvent(sessionId, "PAYEE_LOOKUP", { payee_name: payidRaw, exists: false, by: "payid", directory_match: directoryMatch });
        if (!directoryMatch) return await blockedResponse(sessionId, intent, `"${payidRaw}" is not a valid, registered PayID.`);
        payeeRecord = await provisionDirectoryPayee(sessionId, intent, directoryMatch, [directoryMatch.display_number]);
        exists = true;
      }
      intent.payee_name = payeeRecord.nickname;
      await appendEvent(sessionId, "PAYEE_LOOKUP", { payee_name: intent.payee_name, exists: true, by: "payid" });
    } else {
      const matches = await findPayeesByName(DEMO_USER_ID, intent.payee_name);
      if (matches.length > 1) {
        const candidateLabels = matches.map((m) => (m.phone_number ? `${m.nickname} (${m.phone_number})` : m.nickname));
        await appendEvent(sessionId, "PAYEE_LOOKUP", { payee_name: intent.payee_name, exists: true, ambiguous: true, candidates: candidateLabels });
        const reason = `Multiple contacts match "${intent.payee_name}": ${candidateLabels.join(", ")}. Say the PayID number to pick one, or open Setup and mark them as the same person if they are.`;
        const decision = { decision: "CLARIFY", reason, required_confirmation: "none", risk_level: "low" };
        await appendEvent(sessionId, "DECISION", decision);
        return {
          ok: false, intent, payee_exists: true, decision, candidates: candidateLabels,
          candidate_payees: matches.map((m) => ({ payee_id: m.payee_id, nickname: m.nickname, phone_number: m.phone_number })),
        };
      }
      payeeRecord = matches[0] || null;
      exists = !!payeeRecord;
      if (payeeRecord) intent.payee_name = payeeRecord.nickname;
      await appendEvent(sessionId, "PAYEE_LOOKUP", { payee_name: intent.payee_name, exists });
    }

    const hasReceiverTracking = payeeRecord ? !!payeeRecord.stripe_connected_account_id : false;
    const decision = decideNext(intent, exists, hasReceiverTracking, HARD_CAP_AUD);
    await appendEvent(sessionId, "DECISION", decision);
    if (decision.decision === "CLARIFY" || decision.decision === "BLOCK") {
      return { ok: false, intent, payee_exists: exists, decision };
    }

    const payeeId = payeeRecord ? payeeRecord.payee_id : null;
    const amountCents = Math.round(intent.amount * 100);
    const txn_id = await createPendingTransaction(sessionId, DEMO_USER_ID, amountCents, intent.currency, payeeId);
    await appendEvent(sessionId, "TXN_CREATED", { txn_id, amount_cents: amountCents, currency: intent.currency });
    const conf = await createConfirmation(txn_id, DEMO_USER_ID, decision.required_confirmation);
    await appendEvent(sessionId, "CONFIRMATION_CREATED", conf);

    let payeeInfo = null;
    let payidSuffix = "";
    if (payeeRecord) {
      payeeInfo = {
        payee_id: payeeRecord.payee_id, nickname: payeeRecord.nickname, phone_number: payeeRecord.phone_number,
        has_receiver_tracking: !!payeeRecord.stripe_connected_account_id, is_saved_contact: !!payeeRecord.is_contact,
      };
      if (payeeRecord.phone_number) payidSuffix = ` (PayID: ${payeeRecord.phone_number})`;
    }
    const read_back = `Confirm: Pay ${intent.currency} ${intent.amount.toFixed(2)} to ${intent.payee_name}${payidSuffix}. Required: ${decision.required_confirmation.toUpperCase()}`;
    return { ok: true, rail: "payid", intent, payee_exists: exists, payee: payeeInfo, decision, txn_id, confirmation: conf, read_back };
  }

  // --- /bpay/command + /bpay/execute (port of main.py) ------------------------
  function bpayConfirmationRequirement(amountCents) {
    return amountCents > 5000 ? "pin" : "normal";
  }
  async function bpayCommand(sessionId, text) {
    await appendEvent(sessionId, "BPAY_COMMAND_RECEIVED", { text });
    const parsed = parseBpayCommand(text);
    if (!parsed.ok) return { ok: false, error: "Not a BPAY command" };

    let biller = null;
    if (parsed.biller_code) biller = await lookupBiller(parsed.biller_code);
    else if (parsed.biller_name) biller = (await findSavedBillerByNickname(DEMO_USER_ID, parsed.biller_name)) || (await findBillerByName(parsed.biller_name));

    const missing = new Set(biller ? parsed.missing.filter((m) => m !== "biller") : parsed.missing);
    if (!biller) missing.add("biller_code");
    if (parsed.crn && !validateCrnMod10(parsed.crn)) missing.add("crn");

    const parsedSummary = { biller_name: parsed.biller_name, biller_code: biller ? biller.biller_code : parsed.biller_code, biller_display_name: biller ? biller.biller_name : null, crn: parsed.crn, amount: parsed.amount };
    if (missing.size) {
      const reason = `Missing or invalid: ${[...missing].sort().join(", ")}.`;
      await appendEvent(sessionId, "BPAY_SLOTS_MISSING", { missing: [...missing].sort(), parsed: parsedSummary });
      return { ok: false, decision: { decision: "CLARIFY", reason, required_confirmation: "none", risk_level: "low" }, missing: [...missing].sort(), parsed: parsedSummary };
    }

    const amountCents = Math.round(parsed.amount * 100);
    if (amountCents <= 0) return { ok: false, error: "Amount must be > 0" };
    if (!(biller.min_amount_cents <= amountCents && amountCents <= biller.max_amount_cents)) {
      return { ok: false, error: `Amount is outside ${biller.biller_name}'s allowed range for this biller.` };
    }

    const requiredConfirmation = bpayConfirmationRequirement(amountCents);
    const txn_id = await createPendingBpayTransaction(sessionId, DEMO_USER_ID, amountCents, "AUD", biller.biller_code, parsed.crn);
    await appendEvent(sessionId, "TXN_CREATED", { txn_id, amount_cents: amountCents, currency: "AUD", rail: "bpay" });
    const conf = await createConfirmation(txn_id, DEMO_USER_ID, requiredConfirmation);
    await appendEvent(sessionId, "CONFIRMATION_CREATED", conf);

    const read_back = `Confirm: Pay AUD ${parsed.amount.toFixed(2)} to ${biller.biller_name} (biller code ${biller.biller_code}, CRN ${parsed.crn}). Required: ${requiredConfirmation.toUpperCase()}`;
    return {
      ok: true, rail: "bpay", biller, parsed: parsedSummary,
      decision: { decision: requiredConfirmation === "normal" ? "PROCEED" : "STEP_UP", reason: "BPAY slots resolved", required_confirmation: requiredConfirmation, risk_level: "low" },
      txn_id, confirmation: conf, read_back,
    };
  }
  async function bpayExecute(sessionId, txnId) {
    const txn = await getTransaction(txnId);
    if (!txn) return { ok: false, error: "Transaction not found" };
    if (txn.status !== "confirmed") {
      await appendEvent(sessionId, "PAY_EXECUTE_BLOCKED", { reason: "txn not confirmed", status: txn.status, txn_id: txnId });
      return { ok: false, error: `Transaction status must be confirmed (found ${txn.status})` };
    }
    const simulated_reference = "BPAY-SIM-" + uuid().replace(/-/g, "").slice(0, 12).toUpperCase();
    await updateTransactionStatus(txnId, "succeeded");
    await appendEvent(sessionId, "BPAY_SETTLEMENT_SIMULATED", { txn_id: txnId, biller_code: txn.bpay_biller_code, crn: txn.bpay_crn, amount_cents: txn.amount_cents, simulated_reference });
    return { ok: true, txn_id: txnId, final_status: "succeeded", simulated_reference, note: "BPAY settlement is simulated in this demo." };
  }

  // --- /pay/execute (mocked Stripe, port of main.py pay_execute) -------------
  async function payExecute(sessionId, txnId) {
    const txn = await getTransaction(txnId);
    if (!txn) {
      await appendEvent(sessionId, "PAY_EXECUTE_FAILED", { reason: "txn not found", txn_id: txnId });
      return { ok: false, error: "Transaction not found" };
    }
    if (txn.status !== "confirmed") {
      await appendEvent(sessionId, "PAY_EXECUTE_BLOCKED", { reason: "txn not confirmed", status: txn.status, txn_id: txnId });
      return { ok: false, error: `Transaction status must be confirmed (found ${txn.status})` };
    }
    const pm = (await DB.getAllByIndex("payment_methods", "user_id", txn.user_id)).find((p) => p.is_default);
    if (!pm) {
      await appendEvent(sessionId, "PAY_EXECUTE_FAILED", { reason: "no default payment method" });
      return { ok: false, error: "No default payment method set" };
    }
    const payee = txn.payee_id ? await getPayee(txn.payee_id) : null;
    const destination_account_id = payee ? payee.stripe_connected_account_id : null;
    await appendEvent(sessionId, "PAY_EXECUTE_START", { txn_id: txnId, amount_cents: txn.amount_cents, currency: txn.currency, payment_method_label: pm.label, destination_account_id });

    const pi = mockPaymentIntent();
    txn.stripe_payment_intent_id = pi.id;
    txn.status = "succeeded";
    await DB.put("transactions", txn);
    await appendEvent(sessionId, "PAY_EXECUTE_SUCCESS", { txn_id: txnId, stripe_payment_intent_id: pi.id, stripe_status: pi.status });

    let receiver_evidence = null;
    if (destination_account_id) {
      mockBalances[destination_account_id] = (mockBalances[destination_account_id] || 0) + txn.amount_cents;
      const balance = mockBalanceFor(destination_account_id);
      await appendEvent(sessionId, "RECEIVER_BALANCE_CONFIRMED", { txn_id: txnId, destination_account_id, ...balance });
      receiver_evidence = { payee_nickname: payee.nickname, destination_account_id, stripe_transfer_id: "tr_offline_" + uuid().slice(0, 8), confirmed: true, ...balance };
    }
    return { ok: true, txn_id: txnId, stripe_payment_intent_id: pi.id, stripe_status: pi.status, final_status: "succeeded", receiver_evidence };
  }

  // --- route table -------------------------------------------------------------
  // Mirrors the endpoints app.js calls (see mobile/MOBILE_OFFLINE.md for the
  // full coverage list, including what's deliberately NOT implemented here).
  async function handle(path, { method = "GET", body } = {}) {
    await ready();
    const m = (pattern) => {
      const re = new RegExp("^" + pattern.replace(/\{[^}]+\}/g, "([^/]+)") + "$");
      const match = re.exec(path);
      return match ? match.slice(1) : null;
    };

    if (method === "POST" && path === "/session/new") {
      return { session_id: uuid() };
    }
    if (method === "POST" && path === "/command/text") {
      return await commandText(body.session_id, body.text);
    }
    if (method === "POST" && path === "/bpay/command") {
      return await bpayCommand(body.session_id, body.text);
    }
    if (method === "POST" && path === "/bpay/execute") {
      return await bpayExecute(body.session_id, body.txn_id);
    }
    if (method === "GET" && path === "/bpay/billers") {
      return { billers: await listSavedBillers(DEMO_USER_ID) };
    }
    if (method === "POST" && path === "/pay/execute") {
      return await payExecute(body.session_id, body.txn_id);
    }
    if (method === "POST" && path === "/confirm/normal") {
      const conf = await getConfirmation(body.confirmation_id);
      if (!conf) return { ok: false, error: "Confirmation not found" };
      if (conf.status !== "pending") return { ok: false, error: `Confirmation is ${conf.status}` };
      if (isConfirmationExpired(conf)) {
        await setConfirmationStatus(conf.confirmation_id, "expired");
        await updateTransactionStatus(conf.txn_id, "failed");
        return { ok: false, error: "Confirmation expired" };
      }
      if ((body.phrase || "").trim().toUpperCase() !== "CONFIRM") return { ok: false, error: "Wrong phrase" };
      await setConfirmationStatus(body.confirmation_id, "approved");
      await updateTransactionStatus(conf.txn_id, "confirmed");
      await appendEvent(body.session_id, "CONFIRM_APPROVED", { confirmation_id: body.confirmation_id, txn_id: conf.txn_id });
      return { ok: true, txn_id: conf.txn_id, status: "confirmed" };
    }
    if (method === "POST" && path === "/confirm/pin") {
      const conf = await getConfirmation(body.confirmation_id);
      if (!conf) return { ok: false, error: "Confirmation not found" };
      if (conf.status !== "pending") return { ok: false, error: `Confirmation is ${conf.status}` };
      if (isConfirmationExpired(conf)) {
        await setConfirmationStatus(conf.confirmation_id, "expired");
        await updateTransactionStatus(conf.txn_id, "failed");
        return { ok: false, error: "Confirmation expired" };
      }
      const user = await DB.get("users", conf.user_id);
      if (!user || !user.pin_hash) return { ok: false, error: "PIN not set for user" };
      if (body.pin !== user.pin_hash) {
        const attempts = await incrementAttempts(body.confirmation_id);
        if (attempts >= conf.max_attempts) {
          await setConfirmationStatus(body.confirmation_id, "rejected");
          await updateTransactionStatus(conf.txn_id, "failed");
          return { ok: false, error: "Too many attempts. Confirmation rejected." };
        }
        return { ok: false, error: "Wrong PIN" };
      }
      await setConfirmationStatus(body.confirmation_id, "approved");
      await updateTransactionStatus(conf.txn_id, "confirmed");
      await appendEvent(body.session_id, "CONFIRM_APPROVED", { confirmation_id: body.confirmation_id, txn_id: conf.txn_id });
      return { ok: true, txn_id: conf.txn_id, status: "confirmed" };
    }
    if (method === "POST" && path === "/confirm/cancel") {
      const conf = await getConfirmation(body.confirmation_id);
      if (!conf) return { ok: false, error: "Confirmation not found" };
      if (conf.status !== "pending") return { ok: false, error: `Confirmation is ${conf.status}` };
      await setConfirmationStatus(body.confirmation_id, "cancelled");
      await updateTransactionStatus(conf.txn_id, "failed");
      await appendEvent(body.session_id, "CONFIRM_CANCELLED", { confirmation_id: body.confirmation_id, txn_id: conf.txn_id });
      return { ok: true, txn_id: conf.txn_id };
    }

    let mm = m("/payees/{id}/payids");
    if (mm && method === "GET") return { payids: await listPayids(mm[0]) };
    if (mm && method === "POST") {
      if (!(await getPayee(mm[0]))) return { ok: false, error: "Unknown contact" };
      return await addPayid(DEMO_USER_ID, mm[0], body.value, body.label);
    }
    mm = m("/payees/{id}/balance");
    if (mm && method === "GET") {
      const payee = await getPayee(mm[0]);
      if (!payee) return { ok: false, error: "Payee not found" };
      if (!payee.stripe_connected_account_id) return { ok: false, error: "This payee has no receiver account to check" };
      return { ok: true, payee_id: mm[0], ...mockBalanceFor(payee.stripe_connected_account_id) };
    }
    mm = m("/payees/{id}/save_as_contact");
    if (mm && method === "POST") {
      const payee = await getPayee(mm[0]);
      if (!payee) return { ok: false, error: "Payee not found" };
      const nickname = (body?.nickname || payee.nickname).trim();
      let linked_contact_id = payee.linked_contact_id;
      const existing = await findSavedContactByNickname(DEMO_USER_ID, nickname);
      const isSameNumber = existing && normalizePayid(existing.phone_number || "") === normalizePayid(payee.phone_number || "");
      if (existing && existing.payee_id !== mm[0] && !isSameNumber) {
        if (body?.resolution === "same_person") linked_contact_id = existing.linked_contact_id || existing.payee_id;
        else if (body?.resolution === "different_person") linked_contact_id = null;
        else {
          return { ok: false, conflict: "duplicate_name", existing_contact: { payee_id: existing.payee_id, nickname: existing.nickname, phone_number: existing.phone_number }, error: `You already have a contact named "${nickname}" (${existing.phone_number || "no PayID"}). Is this the same person (add as another number for them), or someone different (save under a different name)?` };
        }
      }
      payee.is_contact = 1;
      payee.nickname = nickname;
      payee.linked_contact_id = linked_contact_id;
      await DB.put("payees", payee);
      return { ok: true, payee_id: mm[0], nickname };
    }

    if (method === "GET" && path === "/payees") {
      const rows = (await DB.getAllByIndex("payees", "user_id", DEMO_USER_ID)).filter((r) => r.is_contact).sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
      return { payees: rows };
    }
    if (method === "POST" && path === "/payees/add_contact") {
      const nickname = (body.nickname || "").trim();
      const phone_number = (body.phone_number || "").trim();
      if (!nickname || !phone_number) return { ok: false, error: "nickname and phone_number are required" };
      let linked_contact_id = null;
      const existing = await findSavedContactByNickname(DEMO_USER_ID, nickname);
      const isSameNumber = existing && normalizePayid(existing.phone_number || "") === normalizePayid(phone_number);
      if (existing && !isSameNumber) {
        if (body.resolution === "same_person") linked_contact_id = existing.linked_contact_id || existing.payee_id;
        else if (body.resolution === "different_person") linked_contact_id = null;
        else return { ok: false, conflict: "duplicate_name", existing_contact: { payee_id: existing.payee_id, nickname: existing.nickname, phone_number: existing.phone_number }, error: `You already have a contact named "${nickname}" (${existing.phone_number || "no PayID"}). Is this the same person (add as another number for them), or someone different (save under a different name)?` };
      }
      const connected_account_id = mockConnectedAccount();
      const payee_id = uuid();
      const now = nowIso();
      await DB.put("payees", { payee_id, user_id: DEMO_USER_ID, nickname, type: "stripe", identifier: phone_number, phone_number, stripe_connected_account_id: connected_account_id, is_contact: 1, linked_contact_id, created_at: now });
      await addPayid(DEMO_USER_ID, payee_id, phone_number);
      return { ok: true, payee_id, phone_number, stripe_connected_account_id: connected_account_id };
    }
    if (method === "POST" && path === "/payees/seed_demo_contacts") {
      const DEMO_CONTACTS = [["Alice Wonderland", "0400 111 222"], ["Bob Marley", "0400 333 444"], ["Charlie Chaplin", "0400 555 666"]];
      const created = [];
      const skipped = [];
      for (const [nickname, phone_number] of DEMO_CONTACTS) {
        if (await payeeExistsForUser(DEMO_USER_ID, nickname)) {
          skipped.push(nickname);
          continue;
        }
        const result = await handle("/payees/add_contact", { method: "POST", body: { nickname, phone_number } });
        if (result.ok) created.push({ nickname, ...result });
        else skipped.push(nickname);
      }
      return { ok: true, created, skipped };
    }

    if (method === "GET" && path === "/payment_methods") {
      return { payment_methods: await DB.getAllByIndex("payment_methods", "user_id", DEMO_USER_ID) };
    }
    if (method === "POST" && path === "/payment_methods/seed_test_card") {
      const pm_id = uuid();
      const now = nowIso();
      const existing = await DB.getAllByIndex("payment_methods", "user_id", DEMO_USER_ID);
      for (const pm of existing) {
        pm.is_default = 0;
        await DB.put("payment_methods", pm);
      }
      await DB.put("payment_methods", { pm_id, user_id: DEMO_USER_ID, label: "Test Visa", stripe_customer_id: "cus_offline_" + uuid().slice(0, 8), stripe_payment_method_id: "pm_offline_" + uuid().slice(0, 8), is_default: 1, created_at: now });
      return { ok: true, pm_id, is_default: true, label: "Test Visa" };
    }

    if (method === "GET" && path.startsWith("/audit/") && path.endsWith("/events")) {
      const sessionId = path.split("/")[2];
      const events = (await DB.getAllByIndex("audit_events", "session_id", sessionId)).sort((a, b) => (a.ts < b.ts ? -1 : 1));
      return { events };
    }
    if (method === "GET" && path.startsWith("/audit/") && path.endsWith("/verify")) {
      const sessionId = path.split("/")[2];
      return await verifySessionChain(sessionId);
    }

    if (method === "POST" && path === "/support/chat") {
      // No LLM runs on-device in this build — see MOBILE_OFFLINE.md.
      return { ok: true, reply: "Support chat isn't available in the offline app yet — payments, PayIDs and BPAY all still work without it." };
    }
    if (method === "POST" && path === "/services/command") {
      return { ok: false, error: "Service bill payments aren't available in the offline app yet." };
    }
    if (path.startsWith("/webauthn/") || path === "/confirm/passkey") {
      return { ok: false, error: "Passkeys aren't available in the offline app — use your PIN instead." };
    }

    return { ok: false, error: `Offline app has no handler for ${method} ${path}` };
  }

  window.VPOffline = { handle, ready, DEMO_USER_ID };
})();
