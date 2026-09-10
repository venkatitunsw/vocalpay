// ---------------------------------------------------------------------------
// VocalPay chat frontend — plain JS, no build step. Talks to the FastAPI
// backend defined in main.py.
// ---------------------------------------------------------------------------

// Same-origin by default: main.py serves this frontend directly (mounted as
// static files), so relative paths reach the API whether that's
// 127.0.0.1:8000 locally or the deployed cloud URL — no config needed either
// way. Only set this if the frontend is ever hosted separately from the API.
const API_BASE = "";

const state = {
  sessionId: null,
  panelOpen: false,
  activeTab: "setup", // "setup" | "audit"
};

const feed = document.getElementById("feed");
const sessionBadge = document.getElementById("session-badge");
const composer = document.getElementById("composer");
const textInput = document.getElementById("text-input");
const sendBtn = document.getElementById("send-btn");
const setupToggle = document.getElementById("setup-toggle");
const auditToggle = document.getElementById("audit-toggle");
const sidePanel = document.getElementById("side-panel");
const panelSetup = document.getElementById("panel-setup");
const panelAudit = document.getElementById("panel-audit");
const auditEvents = document.getElementById("audit-events");
const chainStatus = document.getElementById("chain-status");
const payeeList = document.getElementById("payee-list");
const pmList = document.getElementById("pm-list");
const addPayeeForm = document.getElementById("add-payee-form");
const payeeNicknameInput = document.getElementById("payee-nickname");
const payeePhoneInput = document.getElementById("payee-phone");
const payeeFeedback = document.getElementById("payee-feedback");
const seedContactsBtn = document.getElementById("seed-contacts-btn");
const seedContactsFeedback = document.getElementById("seed-contacts-feedback");
const seedCardBtn = document.getElementById("seed-card-btn");
const pmFeedback = document.getElementById("pm-feedback");
const micBtn = document.getElementById("mic-btn");
const micPulse = document.getElementById("mic-pulse");
const micStatus = document.getElementById("mic-status");

// --- API helper -------------------------------------------------------------

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    throw new Error(`${method} ${path} -> HTTP ${res.status}`);
  }
  return res.json();
}

// --- Feed rendering helpers --------------------------------------------------

function scrollToBottom() {
  feed.scrollTop = feed.scrollHeight;
}

function appendUserBubble(text) {
  const el = document.createElement("div");
  el.className = "flex justify-end";
  el.innerHTML = `
    <div class="max-w-[80%] bg-emerald-500 text-slate-950 rounded-2xl rounded-br-sm px-4 py-2.5 text-sm font-medium">
      ${escapeHtml(text)}
    </div>`;
  feed.appendChild(el);
  scrollToBottom();
}

function appendAssistantBubble(text) {
  const el = document.createElement("div");
  el.className = "flex justify-start";
  el.innerHTML = `
    <div class="max-w-[80%] bg-slate-800 rounded-2xl rounded-bl-sm px-4 py-2.5 text-sm">
      ${escapeHtml(text)}
    </div>`;
  feed.appendChild(el);
  scrollToBottom();
  return el;
}

function appendErrorBubble(text) {
  const el = document.createElement("div");
  el.className = "flex justify-start";
  el.innerHTML = `
    <div class="max-w-[80%] bg-rose-950 border border-rose-800 text-rose-200 rounded-2xl rounded-bl-sm px-4 py-2.5 text-sm">
      ${escapeHtml(text)}
    </div>`;
  feed.appendChild(el);
  scrollToBottom();
  return el;
}

// A "card" is a persistent, mutable assistant bubble used for interactive
// system flows (confirmation, payment execution) instead of plain text.
function appendCard() {
  const wrap = document.createElement("div");
  wrap.className = "flex justify-start";
  const card = document.createElement("div");
  card.className = "max-w-[85%] w-full bg-slate-800 border border-slate-700 rounded-2xl rounded-bl-sm p-4 text-sm space-y-3";
  wrap.appendChild(card);
  feed.appendChild(wrap);
  scrollToBottom();
  return card;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function money(amount, currency) {
  return `${currency} ${Number(amount).toFixed(2)}`;
}

// --- Session lifecycle --------------------------------------------------------

async function initSession() {
  try {
    const res = await api("/session/new", { method: "POST" });
    state.sessionId = res.session_id;
    sessionBadge.textContent = state.sessionId.slice(0, 8);
    sessionBadge.title = state.sessionId;
    appendAssistantBubble(
      "Hi, I'm VocalPay. Tell me a payment to make — e.g. “Pay 12 to John for dinner”, or pay a PayID directly, e.g. “Pay 12 to 0412 345 678”."
    );
  } catch (err) {
    sessionBadge.textContent = "offline";
    appendErrorBubble(`Couldn't reach the backend at ${API_BASE}. Is uvicorn running? (${err.message})`);
  }
}

// --- Command flow --------------------------------------------------------------

composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = textInput.value.trim();
  if (!text || !state.sessionId) return;

  appendUserBubble(text);
  textInput.value = "";
  sendBtn.disabled = true;

  try {
    const res = await api("/command/text", {
      method: "POST",
      body: { session_id: state.sessionId, text },
    });
    handleCommandResponse(res);
  } catch (err) {
    appendErrorBubble(`Request failed: ${err.message}`);
  } finally {
    sendBtn.disabled = false;
    refreshAuditIfOpen();
  }
});

function handleCommandResponse(res) {
  if (!res.ok) {
    // Parse failure (no `decision`) vs. a policy CLARIFY/BLOCK (has `decision`)
    if (res.decision) {
      appendErrorBubble(
        `${res.decision.decision}: ${res.decision.reason}`
      );
    } else {
      appendErrorBubble(res.error || "Sorry, I couldn't understand that.");
    }
    return;
  }

  appendAssistantBubble(res.read_back);
  renderConfirmationCard(res);
}

// --- Confirmation card (normal phrase or PIN) -----------------------------------

function renderConfirmationCard(cmdRes) {
  const card = appendCard();
  const { intent, txn_id, confirmation, payee } = cmdRes;
  const required = confirmation.required_confirmation;

  const header = `
    <div class="flex items-center justify-between">
      <span class="text-xs uppercase tracking-wide text-slate-400">Confirm payment</span>
      <span class="text-[11px] px-2 py-0.5 rounded-full ${required === "pin" ? "bg-amber-500/20 text-amber-300" : "bg-slate-700 text-slate-300"}">
        ${required === "pin" ? "PIN required" : "Type CONFIRM"}
      </span>
    </div>
    <div class="text-lg font-semibold">${money(intent.amount, intent.currency)} → ${escapeHtml(intent.payee_name)}</div>
    ${payee && payee.phone_number ? `<div class="text-xs text-slate-400 font-mono">PayID: ${escapeHtml(payee.phone_number)}</div>` : ""}
    ${intent.note ? `<div class="text-xs text-slate-400">"${escapeHtml(intent.note)}"</div>` : ""}
    ${
      payee && !payee.has_receiver_tracking
        ? `<div class="text-[11px] text-amber-400/80">No receiver tracking for this payee — payment will succeed but can't be proven to reach them.</div>`
        : ""
    }
    ${
      payee && payee.has_receiver_tracking && payee.is_saved_contact === false
        ? `<div class="save-contact-row flex items-center justify-between gap-2 text-[11px] bg-slate-900/60 border border-slate-700 rounded-lg px-2.5 py-2">
             <span class="text-slate-400">Not saved to your contacts — this payment will still go through.</span>
             <button type="button" class="save-contact-btn shrink-0 text-emerald-400 hover:text-emerald-300 font-medium underline underline-offset-2" data-payee-id="${payee.payee_id}">Save contact</button>
           </div>`
        : ""
    }
  `;

  if (required === "pin") {
    card.innerHTML = header + pinFormHtml();
    wirePinForm(card, txn_id, confirmation.confirmation_id);
  } else {
    card.innerHTML = header + normalFormHtml();
    wireNormalForm(card, txn_id, confirmation.confirmation_id);
  }

  const saveBtn = card.querySelector(".save-contact-btn");
  if (saveBtn) wireSaveContactButton(saveBtn, payee.nickname);
}

function wireSaveContactButton(btn, nickname) {
  btn.addEventListener("click", () => attemptSaveAsContact(btn, nickname));
}

async function attemptSaveAsContact(btn, nickname, resolution, overrideNickname) {
  btn.disabled = true;
  const row = btn.closest(".save-contact-row");
  const nicknameToSave = overrideNickname || nickname;
  try {
    const body = resolution ? { resolution, nickname: nicknameToSave } : {};
    const res = await api(`/payees/${btn.dataset.payeeId}/save_as_contact`, { method: "POST", body });
    if (res.ok) {
      row.innerHTML = `<span class="text-emerald-400 font-medium">Saved "${escapeHtml(res.nickname)}" to contacts.</span>`;
      loadPayees();
      return;
    }
    if (res.conflict === "duplicate_name") {
      renderDuplicateNameConflict(row, res, nicknameToSave, (newName, newResolution) =>
        attemptSaveAsContact(btn, nickname, newResolution, newName)
      );
      return;
    }
    row.innerHTML = `<span class="text-rose-400">${escapeHtml(res.error || "Failed to save contact.")}</span>`;
    btn.disabled = false;
  } catch (err) {
    row.innerHTML = `<span class="text-rose-400">Failed: ${escapeHtml(err.message)}</span>`;
    btn.disabled = false;
  }
}

function normalFormHtml() {
  return `
    <form class="normal-form flex items-center gap-2">
      <input type="text" placeholder="Type CONFIRM"
        class="flex-1 bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-emerald-500 uppercase placeholder:normal-case" />
      <button type="submit" class="bg-emerald-500 hover:bg-emerald-400 text-slate-950 text-sm font-medium px-4 py-2 rounded-lg transition">
        Confirm
      </button>
    </form>
    <p class="feedback text-xs h-4"></p>
  `;
}

function pinFormHtml() {
  return `
    <form class="pin-form space-y-2">
      <div class="flex gap-2">
        ${[0, 1, 2, 3].map((i) => `<input class="pin-box" type="password" inputmode="numeric" maxlength="1" data-idx="${i}" />`).join("")}
        <button type="submit" class="ml-auto bg-emerald-500 hover:bg-emerald-400 text-slate-950 text-sm font-medium px-4 py-2 rounded-lg transition self-center">
          Confirm
        </button>
      </div>
    </form>
    <p class="feedback text-xs h-4"></p>
  `;
}

function wireNormalForm(card, txnId, confirmationId) {
  const form = card.querySelector(".normal-form");
  const input = form.querySelector("input");
  const feedback = card.querySelector(".feedback");
  input.focus();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const phrase = input.value.trim();
    if (!phrase) return;
    setFormBusy(form, true);
    try {
      const res = await api("/confirm/normal", {
        method: "POST",
        body: { session_id: state.sessionId, confirmation_id: confirmationId, phrase },
      });
      await handleConfirmResult(card, form, feedback, res, txnId);
    } catch (err) {
      feedback.textContent = `Request failed: ${err.message}`;
      feedback.className = "feedback text-xs h-4 text-rose-400";
    } finally {
      setFormBusy(form, false);
      refreshAuditIfOpen();
    }
  });
}

function wirePinForm(card, txnId, confirmationId) {
  const form = card.querySelector(".pin-form");
  const boxes = [...form.querySelectorAll(".pin-box")];
  const feedback = card.querySelector(".feedback");
  boxes[0].focus();

  boxes.forEach((box, i) => {
    box.addEventListener("input", () => {
      box.value = box.value.replace(/\D/g, "").slice(0, 1);
      if (box.value && i < boxes.length - 1) boxes[i + 1].focus();
    });
    box.addEventListener("keydown", (e) => {
      if (e.key === "Backspace" && !box.value && i > 0) boxes[i - 1].focus();
    });
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const pin = boxes.map((b) => b.value).join("");
    if (pin.length !== 4) {
      feedback.textContent = "Enter all 4 digits.";
      feedback.className = "feedback text-xs h-4 text-amber-400";
      return;
    }
    setFormBusy(form, true);
    try {
      const res = await api("/confirm/pin", {
        method: "POST",
        body: { session_id: state.sessionId, confirmation_id: confirmationId, pin },
      });
      if (!res.ok) {
        boxes.forEach((b) => (b.value = ""));
        boxes[0].focus();
      }
      await handleConfirmResult(card, form, feedback, res, txnId);
    } catch (err) {
      feedback.textContent = `Request failed: ${err.message}`;
      feedback.className = "feedback text-xs h-4 text-rose-400";
    } finally {
      setFormBusy(form, false);
      refreshAuditIfOpen();
    }
  });
}

function setFormBusy(form, busy) {
  [...form.elements].forEach((el) => (el.disabled = busy));
}

async function handleConfirmResult(card, form, feedback, res, txnId) {
  if (!res.ok) {
    feedback.textContent = res.error || "Confirmation failed.";
    feedback.className = "feedback text-xs h-4 text-rose-400";
    if (res.error && res.error.includes("Too many attempts")) {
      setFormBusy(form, true);
    }
    return;
  }

  feedback.textContent = "";
  form.outerHTML = `<div class="text-emerald-400 text-xs font-medium flex items-center gap-1.5">
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4"><path d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4Z"/></svg>
    Confirmed
  </div>`;

  const payBtn = document.createElement("button");
  payBtn.className = "w-full bg-emerald-500 hover:bg-emerald-400 text-slate-950 text-sm font-semibold py-2.5 rounded-lg transition";
  payBtn.textContent = "Pay now";
  payBtn.addEventListener("click", () => executePayment(card, payBtn, txnId));
  card.appendChild(payBtn);
}

// --- Payment execution -------------------------------------------------------

async function executePayment(card, payBtn, txnId) {
  payBtn.disabled = true;
  payBtn.innerHTML = `<span class="inline-flex items-center gap-2 justify-center w-full">
    <span class="spinner"></span> Authorizing through Stripe…
  </span>`;

  try {
    const res = await api("/pay/execute", {
      method: "POST",
      body: { session_id: state.sessionId, txn_id: txnId },
    });

    if (res.ok) {
      payBtn.outerHTML = `
        <div class="bg-emerald-950 border border-emerald-800 rounded-lg px-3 py-2.5 text-sm space-y-2">
          <div>
            <div class="text-emerald-300 font-medium">Payment ${escapeHtml(res.final_status)}</div>
            <div class="text-[11px] text-emerald-500/80 font-mono mt-0.5">${escapeHtml(res.stripe_payment_intent_id)}</div>
          </div>
          ${renderReceiverEvidence(res.receiver_evidence)}
        </div>`;
    } else {
      payBtn.outerHTML = `
        <div class="bg-rose-950 border border-rose-800 rounded-lg px-3 py-2.5 text-sm">
          <div class="text-rose-300 font-medium">${escapeHtml(res.error || "Payment failed")}</div>
          ${res.details ? `<div class="text-[11px] text-rose-400/80 mt-0.5">${escapeHtml(res.details)}</div>` : ""}
        </div>`;
    }
  } catch (err) {
    payBtn.outerHTML = `<div class="bg-rose-950 border border-rose-800 rounded-lg px-3 py-2.5 text-sm text-rose-300">Request failed: ${escapeHtml(err.message)}</div>`;
  } finally {
    refreshAuditIfOpen();
  }
}

function renderReceiverEvidence(ev) {
  if (!ev) {
    return `<div class="text-[11px] text-slate-500 border-t border-emerald-900 pt-2">
      No receiver tracking for this payee — this only proves the card was charged, not that a specific
      person received it. Add them as a contact in Setup to get receiver evidence.
    </div>`;
  }
  const amount = money(ev.pending_cents / 100 + ev.available_cents / 100, ev.currency.toUpperCase());
  if (ev.confirmed) {
    return `
      <div class="border-t border-emerald-900 pt-2">
        <div class="text-emerald-300 text-xs font-semibold flex items-center gap-1.5">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-3.5 w-3.5"><path d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4Z"/></svg>
          Received by ${escapeHtml(ev.payee_nickname)}
        </div>
        <div class="text-[11px] text-emerald-500/80 mt-1">
          ${amount} now in their Stripe balance (their account, not the platform's).
        </div>
        <div class="text-[10px] text-slate-500 font-mono mt-1 truncate" title="${escapeHtml(ev.destination_account_id)}">
          ${escapeHtml(ev.destination_account_id)}${ev.stripe_transfer_id ? " · " + escapeHtml(ev.stripe_transfer_id) : ""}
        </div>
      </div>`;
  }
  return `
    <div class="border-t border-amber-900 pt-2">
      <div class="text-amber-300 text-xs font-semibold">Receipt not yet confirmed</div>
      <div class="text-[11px] text-amber-500/80 mt-1">
        The charge succeeded but the receiver's balance hasn't updated yet — check again from Setup shortly.
      </div>
    </div>`;
}

// --- Side panel (Setup / Audit Log tabs) ----------------------------------------

function openPanel(tab) {
  state.panelOpen = true;
  state.activeTab = tab;
  sidePanel.classList.remove("-mr-96");
  panelSetup.classList.toggle("hidden", tab !== "setup");
  panelSetup.classList.toggle("flex", tab === "setup");
  panelAudit.classList.toggle("hidden", tab !== "audit");
  panelAudit.classList.toggle("flex", tab === "audit");
  setupToggle.classList.toggle("bg-slate-800", tab === "setup");
  auditToggle.classList.toggle("bg-slate-800", tab === "audit");
  if (tab === "setup") loadSetupData();
  if (tab === "audit") refreshAudit();
}

function closePanel() {
  state.panelOpen = false;
  sidePanel.classList.add("-mr-96");
  setupToggle.classList.remove("bg-slate-800");
  auditToggle.classList.remove("bg-slate-800");
}

setupToggle.addEventListener("click", () => {
  if (state.panelOpen && state.activeTab === "setup") closePanel();
  else openPanel("setup");
});

auditToggle.addEventListener("click", () => {
  if (state.panelOpen && state.activeTab === "audit") closePanel();
  else openPanel("audit");
});

function refreshAuditIfOpen() {
  if (state.panelOpen && state.activeTab === "audit") refreshAudit();
}

// --- Setup tab: payees & payment methods ----------------------------------------

async function loadSetupData() {
  await Promise.all([loadPayees(), loadPaymentMethods()]);
}

async function loadPayees() {
  try {
    const res = await api("/payees");
    renderPayeeList(res.payees);
  } catch (err) {
    payeeList.innerHTML = `<p class="text-rose-400 text-xs">Failed to load: ${escapeHtml(err.message)}</p>`;
  }
}

function renderPayeeList(payees) {
  if (!payees.length) {
    payeeList.innerHTML = `<p class="text-slate-500 text-xs">No contacts yet — messages to a new name will require a PIN.</p>`;
    return;
  }
  payeeList.innerHTML = payees
    .map((p) => {
      const tracked = Boolean(p.stripe_connected_account_id);
      return `
      <div class="bg-slate-800/60 border border-slate-800 rounded-lg px-3 py-2 text-xs">
        <div class="flex items-center justify-between">
          <span class="font-medium">${escapeHtml(p.nickname)}</span>
          <span class="${tracked ? "text-emerald-400" : "text-slate-500"} text-[10px] font-semibold uppercase">
            ${tracked ? "receiver tracked" : escapeHtml(p.type)}
          </span>
        </div>
        ${p.phone_number ? `<div class="text-slate-500 font-mono mt-0.5">PayID: ${escapeHtml(p.phone_number)}</div>` : ""}
        ${p.linked_contact_id ? `<div class="text-sky-400/80 mt-0.5">Same person as another saved number</div>` : ""}
        ${tracked ? `<button class="check-balance-btn text-emerald-400 hover:text-emerald-300 mt-1 underline underline-offset-2" data-payee-id="${p.payee_id}">Check receiver balance</button>
        <div class="balance-result text-slate-400 mt-1"></div>` : ""}
      </div>`;
    })
    .join("");

  payeeList.querySelectorAll(".check-balance-btn").forEach((btn) => {
    btn.addEventListener("click", () => checkReceiverBalance(btn));
  });
}

async function checkReceiverBalance(btn) {
  const payeeId = btn.dataset.payeeId;
  const resultEl = btn.nextElementSibling;
  btn.disabled = true;
  resultEl.textContent = "Checking…";
  try {
    const res = await api(`/payees/${payeeId}/balance`);
    if (res.ok) {
      const amount = money(res.pending_cents / 100 + res.available_cents / 100, res.currency.toUpperCase());
      resultEl.textContent = `${amount} in their Stripe balance (pending ${res.pending_cents}c, available ${res.available_cents}c).`;
      resultEl.className = "balance-result text-emerald-400 mt-1";
    } else {
      resultEl.textContent = res.error || "Could not check balance.";
      resultEl.className = "balance-result text-rose-400 mt-1";
    }
  } catch (err) {
    resultEl.textContent = `Failed: ${err.message}`;
    resultEl.className = "balance-result text-rose-400 mt-1";
  } finally {
    btn.disabled = false;
  }
}

addPayeeForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const nickname = payeeNicknameInput.value.trim();
  const phoneNumber = payeePhoneInput.value.trim();
  if (!nickname || !phoneNumber) {
    payeeFeedback.textContent = "Name and phone number are both required.";
    payeeFeedback.className = "text-[11px] h-4 mt-1 text-amber-400";
    return;
  }
  await attemptAddContact(nickname, phoneNumber);
});

async function attemptAddContact(nickname, phoneNumber, resolution) {
  payeeFeedback.textContent = "Creating receiver account…";
  payeeFeedback.className = "text-[11px] h-4 mt-1 text-slate-400";
  addPayeeForm.querySelector("button").disabled = true;
  try {
    const res = await api("/payees/add_contact", {
      method: "POST",
      body: { nickname, phone_number: phoneNumber, ...(resolution ? { resolution } : {}) },
    });
    if (res.ok) {
      payeeNicknameInput.value = "";
      payeePhoneInput.value = "";
      payeeFeedback.textContent = `Added "${nickname}" with a tracked receiver account.`;
      payeeFeedback.className = "text-[11px] h-4 mt-1 text-emerald-400";
      loadPayees();
    } else if (res.conflict === "duplicate_name") {
      renderDuplicateNameConflict(payeeFeedback, res, nickname, (newName, newResolution) =>
        attemptAddContact(newName, phoneNumber, newResolution)
      );
    } else {
      payeeFeedback.textContent = res.error || "Failed to add contact.";
      payeeFeedback.className = "text-[11px] h-4 mt-1 text-rose-400";
    }
  } catch (err) {
    payeeFeedback.textContent = `Failed: ${err.message}`;
    payeeFeedback.className = "text-[11px] h-4 mt-1 text-rose-400";
  } finally {
    addPayeeForm.querySelector("button").disabled = false;
  }
}

// A name colliding with an existing saved contact under a different number is
// genuinely ambiguous in real life — two different people can share a name,
// or one person can have a second number. Rather than silently creating a
// confusing duplicate, offer both resolutions explicitly.
function renderDuplicateNameConflict(container, res, nickname, onResolve) {
  container.innerHTML = `
    <div class="text-amber-400 mb-1">${escapeHtml(res.error)}</div>
    <div class="flex flex-wrap gap-x-2 gap-y-1 items-center">
      <button type="button" class="conflict-same text-emerald-400 hover:text-emerald-300 underline underline-offset-2">Same person — add as another number</button>
      <span class="text-slate-600">|</span>
      <button type="button" class="conflict-diff-toggle text-sky-400 hover:text-sky-300 underline underline-offset-2">Different person — rename</button>
    </div>
    <div class="conflict-rename-row hidden flex gap-2 mt-1.5">
      <input type="text" class="conflict-rename-input flex-1 bg-slate-900 border border-slate-700 rounded-lg px-2 py-1 text-xs outline-none focus:ring-2 focus:ring-sky-500" value="${escapeHtml(nickname)} (2)" />
      <button type="button" class="conflict-rename-save bg-sky-500 hover:bg-sky-400 text-slate-950 text-xs font-medium px-3 py-1 rounded-lg">Save</button>
    </div>
  `;
  container.className = "text-[11px] mt-1";

  container.querySelector(".conflict-same").addEventListener("click", () => onResolve(nickname, "same_person"));
  container.querySelector(".conflict-diff-toggle").addEventListener("click", () => {
    container.querySelector(".conflict-rename-row").classList.remove("hidden");
    container.querySelector(".conflict-rename-input").focus();
  });
  container.querySelector(".conflict-rename-save").addEventListener("click", () => {
    const newName = container.querySelector(".conflict-rename-input").value.trim();
    if (newName) onResolve(newName, "different_person");
  });
}

seedContactsBtn.addEventListener("click", async () => {
  seedContactsBtn.disabled = true;
  seedContactsBtn.textContent = "Seeding contacts…";
  seedContactsFeedback.textContent = "";
  try {
    const res = await api("/payees/seed_demo_contacts", { method: "POST" });
    const createdNames = res.created.map((c) => c.nickname).join(", ");
    seedContactsFeedback.textContent = res.created.length
      ? `Added: ${createdNames}${res.skipped.length ? ` (skipped existing: ${res.skipped.join(", ")})` : ""}`
      : `All demo contacts already exist (${res.skipped.join(", ")}).`;
    seedContactsFeedback.className = "text-[11px] h-4 mt-1 text-emerald-400";
    loadPayees();
  } catch (err) {
    seedContactsFeedback.textContent = `Failed: ${err.message}`;
    seedContactsFeedback.className = "text-[11px] h-4 mt-1 text-rose-400";
  } finally {
    seedContactsBtn.disabled = false;
    seedContactsBtn.textContent = "+ Seed a variety of demo contacts";
  }
});

async function loadPaymentMethods() {
  try {
    const res = await api("/payment_methods");
    renderPmList(res.payment_methods);
  } catch (err) {
    pmList.innerHTML = `<p class="text-rose-400 text-xs">Failed to load: ${escapeHtml(err.message)}</p>`;
  }
}

function renderPmList(pms) {
  if (!pms.length) {
    pmList.innerHTML = `<p class="text-slate-500 text-xs">No payment methods yet — payments can't be executed until one is added.</p>`;
    return;
  }
  pmList.innerHTML = pms
    .map(
      (pm) => `
      <div class="flex items-center justify-between bg-slate-800/60 border border-slate-800 rounded-lg px-3 py-1.5 text-xs">
        <span class="font-medium">${escapeHtml(pm.label)}</span>
        ${pm.is_default ? `<span class="text-emerald-400 text-[10px] font-semibold uppercase">default</span>` : ""}
      </div>`
    )
    .join("");
}

seedCardBtn.addEventListener("click", async () => {
  seedCardBtn.disabled = true;
  seedCardBtn.textContent = "Creating test card…";
  pmFeedback.textContent = "";
  try {
    const res = await api("/payment_methods/seed_test_card", { method: "POST" });
    if (res.ok) {
      pmFeedback.textContent = "Added Test Visa (now default).";
      pmFeedback.className = "text-[11px] h-4 mt-1 text-emerald-400";
      loadPaymentMethods();
    } else {
      pmFeedback.textContent = res.error || "Failed to create test card.";
      pmFeedback.className = "text-[11px] h-4 mt-1 text-rose-400";
    }
  } catch (err) {
    pmFeedback.textContent = `Failed: ${err.message}`;
    pmFeedback.className = "text-[11px] h-4 mt-1 text-rose-400";
  } finally {
    seedCardBtn.disabled = false;
    seedCardBtn.textContent = "+ Add Stripe test card (Visa)";
  }
});

async function refreshAudit() {
  if (!state.sessionId) return;
  try {
    const [eventsRes, verifyRes] = await Promise.all([
      api(`/audit/${state.sessionId}/events`),
      api(`/audit/${state.sessionId}/verify`),
    ]);
    renderAuditEvents(eventsRes.events);
    renderChainStatus(verifyRes);
  } catch (err) {
    chainStatus.textContent = "error";
    chainStatus.className = "text-[11px] font-medium px-2 py-1 rounded-md bg-rose-900 text-rose-300";
  }
}

function renderChainStatus(verifyRes) {
  if (verifyRes.ok) {
    chainStatus.textContent = `verified (${verifyRes.events})`;
    chainStatus.className = "text-[11px] font-medium px-2 py-1 rounded-md bg-emerald-900 text-emerald-300";
  } else {
    chainStatus.textContent = "chain broken";
    chainStatus.className = "text-[11px] font-medium px-2 py-1 rounded-md bg-rose-900 text-rose-300";
  }
}

function renderAuditEvents(events) {
  if (!events.length) {
    auditEvents.innerHTML = `<p class="text-slate-500">No events yet.</p>`;
    return;
  }
  auditEvents.innerHTML = events
    .map((ev, i) => {
      const time = new Date(ev.ts).toLocaleTimeString();
      return `
        <div class="border border-slate-800 rounded-lg p-2.5 ${i === events.length - 1 ? "ring-1 ring-emerald-700/60" : ""}">
          <div class="flex items-center justify-between">
            <span class="font-medium text-slate-200">${escapeHtml(ev.event_type)}</span>
            <span class="text-slate-500">${time}</span>
          </div>
          <div class="font-mono text-[10px] text-slate-500 mt-1 truncate" title="${ev.event_hash}">
            #${ev.event_hash.slice(0, 12)}
          </div>
        </div>`;
    })
    .join("");
  auditEvents.scrollTop = auditEvents.scrollHeight;
}

// --- Voice input (Web Speech API — voice-ready foundation) ---------------------

const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognizer = null;
let listening = false;

if (!SpeechRecognitionImpl) {
  micBtn.disabled = true;
  micBtn.title = "Voice input not supported in this browser";
  micBtn.classList.add("opacity-40", "cursor-not-allowed");
} else {
  recognizer = new SpeechRecognitionImpl();
  recognizer.continuous = false;
  recognizer.interimResults = false;
  recognizer.lang = "en-AU";

  recognizer.onresult = (e) => {
    const transcript = e.results[0][0].transcript;
    textInput.value = transcript;
    textInput.focus();
  };
  recognizer.onerror = () => {
    micStatus.textContent = "Couldn't hear that — try again.";
  };
  recognizer.onend = () => setListening(false);

  micBtn.addEventListener("click", () => {
    if (listening) {
      recognizer.stop();
    } else {
      micStatus.textContent = "";
      recognizer.start();
      setListening(true);
    }
  });
}

function setListening(on) {
  listening = on;
  micPulse.classList.toggle("hidden", !on);
  micBtn.classList.toggle("border-rose-500", on);
  micBtn.classList.toggle("text-rose-400", on);
  micStatus.textContent = on ? "Listening…" : "";
}

// Auto-grow textarea + Enter-to-send (Shift+Enter for newline)
textInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    composer.requestSubmit();
  }
});

// --- Boot ------------------------------------------------------------------

initSession();
