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
  // The most recently rendered confirmation card still awaiting CONFIRM/PIN/
  // passkey, if any — lets the main composer recognize "no actually X" /
  // "cancel" as being about THIS pending payment instead of either trying
  // to parse it as an unrelated new command or asking the chatbot. Cleared
  // the moment that card is confirmed, cancelled, or superseded.
  activeDraft: null, // { confirmationId, txnId, amount, currency } | null
  // Set right after asking "how much?" / "who to?" for an incomplete
  // payment attempt (see VERB_PLUS_BARE_NAME_RE/VERB_PLUS_BARE_AMOUNT_RE
  // below) — lets a bare reply like "30" or "alice" complete that specific
  // question instead of being sent to the chatbot with no context.
  pendingSlotFill: null, // { type: "missing_amount", target } | { type: "missing_target", amount } | null
  // Offline-app only (window.VPOffline): which chat thread is currently
  // open -- "general" (BPAY/support/anything not tied to one contact) or
  // "payee:<id>"/"biller:<code>". Every bubble appended while a thread is
  // open is persisted under this key; see recordBubble() below and
  // offline/offline-engine.js's listThreads/listMessages.
  activeThreadKey: window.VPOffline ? window.VPOffline.GENERAL_THREAD : null,
};

const feed = document.getElementById("feed");
const sessionBadge = document.getElementById("session-badge");
const composer = document.getElementById("composer");
const composerFooter = document.getElementById("composer-footer");
const threadListView = document.getElementById("thread-list-view");
const threadList = document.getElementById("thread-list");
const threadBackBtn = document.getElementById("thread-back-btn");
const headerTitle = document.getElementById("header-title");
const headerSubtitle = document.getElementById("header-subtitle");
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
const passkeyList = document.getElementById("passkey-list");
const registerPasskeyBtn = document.getElementById("register-passkey-btn");
const passkeyFeedback = document.getElementById("passkey-feedback");
const bpayBillerList = document.getElementById("bpay-biller-list");

// --- API helper -------------------------------------------------------------

// The offline build (mobile/) runs entirely on-device: window.VPOffline
// (offline/offline-engine.js) answers every call locally against IndexedDB
// instead of this fetch ever reaching a server. The server build (the
// repo's top-level frontend/) never defines window.VPOffline, so `fetch`
// is the only path there — this file is shared between both via mobile's
// build step copying frontend/ into www/ and adding the offline/ scripts.
async function api(path, { method = "GET", body, signal } = {}) {
  if (window.VPOffline) {
    return await window.VPOffline.handle(path, { method, body });
  }
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal,
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

// Persists one chat bubble into the currently open thread (offline app
// only -- a no-op for the server build, which has no window.VPOffline and
// no thread concept). `replay: true` is set when re-rendering a thread's
// stored history on open, which must never re-persist what it just read.
function recordBubble(role, text, opts = {}) {
  if (!window.VPOffline || opts.replay) return;
  window.VPOffline.recordMessage(state.activeThreadKey, role, text).catch(() => {});
}

// A confirm/pay outcome also lands in the payment's own contact/biller
// thread (card.dataset.threadKey, set in renderConfirmationCard) when
// that's not already where the user is -- see the comment there.
function mirrorBubble(card, role, text) {
  if (!window.VPOffline) return;
  const key = card.dataset.threadKey;
  if (key && key !== state.activeThreadKey) {
    window.VPOffline.recordMessage(key, role, text).catch(() => {});
  }
}

function appendUserBubble(text, opts = {}) {
  const el = document.createElement("div");
  el.className = "flex justify-end";
  el.innerHTML = `
    <div class="max-w-[80%] bg-emerald-500 text-slate-950 rounded-2xl rounded-br-sm px-4 py-2.5 text-sm font-medium">
      ${VocalPrivacy.sens(escapeHtml(text))}
    </div>`;
  feed.appendChild(el);
  scrollToBottom();
  recordBubble("user", text, opts);
}

function appendAssistantBubble(text, opts = {}) {
  const el = document.createElement("div");
  el.className = "flex justify-start";
  const body = opts.sensitive ? VocalPrivacy.sens(escapeHtml(text)) : escapeHtml(text);
  el.innerHTML = `
    <div class="max-w-[80%] bg-slate-800 rounded-2xl rounded-bl-sm px-4 py-2.5 text-sm">
      ${body}
    </div>`;
  if (!opts.replay) VocalPrivacy.speak(text);
  feed.appendChild(el);
  scrollToBottom();
  recordBubble("assistant", text, opts);
  return el;
}

function appendErrorBubble(text, opts = {}) {
  const el = document.createElement("div");
  el.className = "flex justify-start";
  el.innerHTML = `
    <div class="max-w-[80%] bg-rose-950 border border-rose-800 text-rose-200 rounded-2xl rounded-bl-sm px-4 py-2.5 text-sm">
      ${escapeHtml(text)}
    </div>`;
  if (!opts.replay) VocalPrivacy.speak(text);
  feed.appendChild(el);
  scrollToBottom();
  recordBubble("error", text, opts);
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
  return VocalPrivacy.sens(`${currency} ${Number(amount).toFixed(2)}`);
}

// --- Threads (offline app only): one chat per contact, WhatsApp-style ---------
// The landing view is the thread list; opening a thread shows that
// contact's (or "General"'s) persisted history and reveals the composer.
// No-op entirely when window.VPOffline is absent (there's no server build
// of this -- threads only make sense against the on-device history the
// offline app persists; see offline/offline-engine.js's listThreads).

function timeAgoShort(iso) {
  if (!iso) return "";
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

const LOCK_ICON_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4">
  <path d="M12 2a5 5 0 0 0-5 5v3H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-1V7a5 5 0 0 0-5-5Zm-3 8V7a3 3 0 1 1 6 0v3Z"/>
</svg>`;

function threadRowHtml(t) {
  const initial = escapeHtml((t.label || "?").trim().charAt(0).toUpperCase() || "?");
  const preview = t.last_message_preview
    ? VocalPrivacy.sens(escapeHtml(t.last_message_preview))
    : '<span class="text-slate-500">No messages yet — tap to say hello</span>';
  const time = t.last_message_at
    ? `<span class="text-[11px] text-slate-500 shrink-0">${timeAgoShort(t.last_message_at)}</span>`
    : "";
  const hideable = t.thread_key !== window.VPOffline.GENERAL_THREAD;
  const toggleBtn = hideable
    ? `<button type="button" class="thread-hide-toggle shrink-0 h-8 w-8 rounded-lg hover:bg-slate-700 flex items-center justify-center text-slate-500"
         data-thread-key="${escapeHtml(t.thread_key)}" data-hidden="${t.hidden ? "1" : "0"}"
         title="${t.hidden ? "Remove from Private" : "Hide this chat"}" aria-label="${t.hidden ? "Remove from Private" : "Hide this chat"}">${LOCK_ICON_SVG}</button>`
    : "";
  return `
    <div class="flex items-center gap-1">
      <button type="button" class="thread-item flex-1 min-w-0 flex items-center gap-3 px-3 py-2.5 rounded-xl hover:bg-slate-800 text-left transition"
        data-thread-key="${escapeHtml(t.thread_key)}" data-thread-label="${escapeHtml(t.label)}">
        <div class="h-10 w-10 rounded-full bg-slate-700 flex items-center justify-center font-semibold text-sm shrink-0">${initial}</div>
        <div class="min-w-0 flex-1">
          <div class="text-sm font-medium truncate">${escapeHtml(t.label)}</div>
          <div class="text-xs text-slate-400 truncate">${preview}</div>
        </div>
        ${time}
      </button>
      ${toggleBtn}
    </div>`;
}

async function renderThreadList() {
  if (!window.VPOffline) return;
  const threads = await window.VPOffline.listThreads();
  threadList.innerHTML =
    threads.map(threadRowHtml).join("") +
    `<button type="button" id="private-section-btn" class="w-full flex items-center gap-3 px-3 py-3 rounded-xl hover:bg-slate-800 text-left transition mt-2 border-t border-slate-800">
       <div class="h-10 w-10 rounded-full bg-slate-800 flex items-center justify-center shrink-0 text-slate-400">${LOCK_ICON_SVG}</div>
       <div class="text-sm font-medium text-slate-300">Private</div>
     </button>`;
}

async function renderHiddenThreadList() {
  const threads = await window.VPOffline.listHiddenThreads();
  threadList.innerHTML =
    `<button type="button" id="private-back-btn" class="w-full flex items-center gap-2 px-3 py-2.5 rounded-xl hover:bg-slate-800 text-left transition mb-1 text-slate-400 text-sm">
       <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4"><path d="M15.7 4.3a1 1 0 0 1 0 1.4L9.41 12l6.3 6.3a1 1 0 0 1-1.42 1.4l-7-7a1 1 0 0 1 0-1.4l7-7a1 1 0 0 1 1.42 0Z"/></svg>
       Back to Chats
     </button>` +
    (threads.length
      ? threads.map(threadRowHtml).join("")
      : '<p class="text-xs text-slate-500 px-3 py-2">No private chats yet. Tap the lock on any chat to hide it here.</p>');
}

threadList.addEventListener("click", async (e) => {
  if (e.target.closest("#private-section-btn")) {
    const ok = await VocalPrivacy.authenticateBiometric("View private chats");
    if (ok) renderHiddenThreadList();
    return;
  }
  if (e.target.closest("#private-back-btn")) {
    renderThreadList();
    return;
  }
  const toggleBtn = e.target.closest(".thread-hide-toggle");
  if (toggleBtn) {
    const wasHidden = toggleBtn.dataset.hidden === "1";
    if (wasHidden) {
      // Revealing is already gated: you only reach a hidden row's toggle
      // after authenticateBiometric() above, inside the Private section.
      await window.VPOffline.setThreadHidden(toggleBtn.dataset.threadKey, false);
      renderHiddenThreadList();
    } else {
      // Hiding needs no auth -- it's your own device; only *revealing* does.
      await window.VPOffline.setThreadHidden(toggleBtn.dataset.threadKey, true);
      renderThreadList();
    }
    return;
  }
  const btn = e.target.closest(".thread-item");
  if (!btn) return;
  openThread(btn.dataset.threadKey, btn.dataset.threadLabel);
});

async function openThread(threadKey, label) {
  state.activeThreadKey = threadKey;
  threadListView.classList.add("hidden");
  feed.classList.remove("hidden");
  composerFooter.classList.remove("hidden");
  threadBackBtn.classList.remove("hidden");
  threadBackBtn.classList.add("flex");
  headerTitle.textContent = label;
  headerSubtitle.textContent =
    threadKey === window.VPOffline.GENERAL_THREAD ? "BPAY, support & everything else" : "voice-ready payment assistant";

  feed.innerHTML = "";
  const messages = await window.VPOffline.listMessages(threadKey);
  if (!messages.length) {
    appendAssistantBubble(
      threadKey === window.VPOffline.GENERAL_THREAD
        ? "Ask about BPAY, your bills, or anything that's not tied to one contact."
        : `Say a payment to start — e.g. "Pay 12 to ${label}".`,
      { replay: true }
    );
  } else {
    for (const m of messages) {
      if (m.role === "user") appendUserBubble(m.text, { replay: true });
      else if (m.role === "error") appendErrorBubble(m.text, { replay: true });
      else appendAssistantBubble(m.text, { replay: true });
    }
  }
  textInput.focus();
}

function closeThread() {
  state.activeThreadKey = window.VPOffline.GENERAL_THREAD;
  feed.classList.add("hidden");
  composerFooter.classList.add("hidden");
  threadBackBtn.classList.add("hidden");
  threadBackBtn.classList.remove("flex");
  headerTitle.textContent = "VocalPay";
  headerSubtitle.textContent = "voice-ready payment assistant";
  threadListView.classList.remove("hidden");
  renderThreadList();
}

threadBackBtn.addEventListener("click", closeThread);

// --- WebAuthn/passkey helpers --------------------------------------------------
// py_webauthn's options_to_json() encodes binary fields (challenge, user.id,
// credential ids) as base64url strings, but navigator.credentials.create()/
// get() need real ArrayBuffers for those same fields — and the reverse on
// the way back. These convert between the two; there's no bundler here, so
// no @simplewebauthn/browser — this is the same conversion it does.

function base64urlToBuffer(base64url) {
  const padding = "=".repeat((4 - (base64url.length % 4)) % 4);
  const base64 = (base64url + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const buffer = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) buffer[i] = raw.charCodeAt(i);
  return buffer;
}

function bufferToBase64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let str = "";
  for (const b of bytes) str += String.fromCharCode(b);
  return btoa(str).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function registerPasskey(label) {
  const begin = await api("/webauthn/register/begin", { method: "POST" });
  if (!begin.ok) throw new Error(begin.error || "Could not start passkey registration");

  const opts = begin.options;
  const publicKey = {
    ...opts,
    challenge: base64urlToBuffer(opts.challenge),
    user: { ...opts.user, id: base64urlToBuffer(opts.user.id) },
    excludeCredentials: (opts.excludeCredentials || []).map((c) => ({ ...c, id: base64urlToBuffer(c.id) })),
  };

  const credential = await navigator.credentials.create({ publicKey });
  const credentialJson = {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      attestationObject: bufferToBase64url(credential.response.attestationObject),
      clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
      transports: credential.response.getTransports ? credential.response.getTransports() : [],
    },
  };

  return api("/webauthn/register/finish", { method: "POST", body: { credential: credentialJson, label } });
}

async function authenticateWithPasskey(confirmationId) {
  const begin = await api("/webauthn/authenticate/begin", {
    method: "POST",
    body: { confirmation_id: confirmationId },
  });
  if (!begin.ok) throw new Error(begin.error || "Could not start passkey authentication");

  const opts = begin.options;
  const publicKey = {
    ...opts,
    challenge: base64urlToBuffer(opts.challenge),
    allowCredentials: (opts.allowCredentials || []).map((c) => ({ ...c, id: base64urlToBuffer(c.id) })),
  };

  const credential = await navigator.credentials.get({ publicKey });
  const credentialJson = {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      authenticatorData: bufferToBase64url(credential.response.authenticatorData),
      clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
      signature: bufferToBase64url(credential.response.signature),
      userHandle: credential.response.userHandle ? bufferToBase64url(credential.response.userHandle) : null,
    },
  };

  return api("/confirm/passkey", {
    method: "POST",
    body: { session_id: state.sessionId, confirmation_id: confirmationId, credential: credentialJson },
  });
}

// --- Session lifecycle --------------------------------------------------------

async function initSession() {
  const welcomeText =
    "Hi, I'm VocalPay. Tell me a payment to make — e.g. “Pay 12 to John for dinner”, or pay a PayID directly, e.g. “Pay 12 to 0412 345 678”. You can also just ask me things, like “what was my last payment?” or “who are my contacts?”";
  try {
    const res = await api("/session/new", { method: "POST" });
    state.sessionId = res.session_id;
    sessionBadge.textContent = state.sessionId.slice(0, 8);
    sessionBadge.title = state.sessionId;

    if (window.VPOffline) {
      // Thread-list is the landing view (#feed starts hidden) -- seed the
      // General thread's history once instead of rendering straight in.
      const existing = await window.VPOffline.listMessages(window.VPOffline.GENERAL_THREAD);
      if (!existing.length) {
        await window.VPOffline.recordMessage(window.VPOffline.GENERAL_THREAD, "assistant", welcomeText);
      }
      await renderThreadList();
    } else {
      appendAssistantBubble(welcomeText);
    }
  } catch (err) {
    sessionBadge.textContent = "offline";
    threadListView.classList.add("hidden");
    feed.classList.remove("hidden");
    composerFooter.classList.remove("hidden");
    appendErrorBubble(`Couldn't reach the backend at ${API_BASE}. Is uvicorn running? (${err.message})`);
  }
}

// --- Command flow --------------------------------------------------------------

// Only ever consulted when state.activeDraft is set (a confirmation card is
// still open) — these decide whether a composer message is about THAT
// pending payment (cancel it / redirect it) rather than an unrelated new
// command or a chatbot question. Deliberately simple, deterministic pattern
// matching — no LLM involvement in anything that can affect a payment.
// (No "g" flag: these are reused via .test() across many messages, and a
// global flag there would make .test() stateful/unreliable across calls.)
const DRAFT_CANCEL_RE = /\b(cancel|stop|nevermind|never mind|forget it|forget that|scratch that|abort)\b/i;
// A leading "no" (e.g. "No, 0400111222") is anchored to the START of the
// message specifically so it doesn't fire on an unrelated sentence that
// merely contains the word "no" ("I have no idea what you mean").
const DRAFT_CORRECTION_RE = /^\s*no+\b|\b(actually|instead|wait|sorry,?\s*i meant|i meant)\b/i;
const DRAFT_LEADING_NO_RE = /^\s*no+,?\s*/i;
const DRAFT_FILLER_WORDS_RE = /\b(actually|instead|wait|sorry,?\s*i meant|i meant|cancel|stop|nevermind|never mind|forget it|forget that|scratch that|abort)\b/gi;

// A reply that's JUST an amount or JUST a target (name or PayID), nothing
// else — the natural way to answer "How much would you like to pay alice?"
// / "Who would you like to send 20 to?" (state.pendingSlotFill, set above).
const BARE_AMOUNT_ONLY_RE = /^\$?\s*(\d+(?:\.\d+)?)\s*(?:aud|dollars)?$/i;
const BARE_TARGET_ONLY_RE = /^(?:[a-zA-Z][a-zA-Z'-]*(?:\s+[a-zA-Z][a-zA-Z'-]*){0,2}|\d[\d\s]{6,14}\d)$/;

async function tryResolvePendingSlotFill(text) {
  const slotFill = state.pendingSlotFill;
  if (!slotFill) return false;
  state.pendingSlotFill = null; // consumed either way, unless a transition branch below re-sets it

  if (DRAFT_CANCEL_RE.test(text) || DRAFT_LEADING_NO_RE.test(text)) {
    appendAssistantBubble("No worries, cancelled.");
    return true;
  }

  if (slotFill.type === "missing_both") {
    // "pay" alone asked for both pieces at once -- a single bare reply only
    // ever gives us one of them, so narrow down to the other piece instead
    // of dropping the context. Specifically avoids ever shipping a bare
    // name to the chatbot here: it has no tool for "does this contact
    // exist", so it's prone to confidently claiming a real saved contact
    // doesn't exist (observed live) instead of just not understanding.
    if (BARE_AMOUNT_ONLY_RE.test(text)) {
      const amount = text.match(BARE_AMOUNT_ONLY_RE)[1];
      state.pendingSlotFill = { type: "missing_target", amount };
      appendAssistantBubble(`Who would you like to send ${amount} to?`);
      return true;
    }
    if (BARE_TARGET_ONLY_RE.test(text)) {
      const target = text.trim();
      state.pendingSlotFill = { type: "missing_amount", target };
      appendAssistantBubble(`How much would you like to pay ${target}?`);
      return true;
    }
    return false; // not recognizable as either piece -- treat as a new message instead
  }

  let retryText = null;
  if (slotFill.type === "missing_amount" && BARE_AMOUNT_ONLY_RE.test(text)) {
    retryText = `Pay ${text.match(BARE_AMOUNT_ONLY_RE)[1]} to ${slotFill.target}`;
  } else if (slotFill.type === "missing_target" && BARE_TARGET_ONLY_RE.test(text)) {
    retryText = `Pay ${slotFill.amount} to ${text.trim()}`;
  }

  if (!retryText) return false; // didn't look like an answer -- treat as a brand-new message instead

  const retryRes = await api("/command/text", {
    method: "POST",
    body: { session_id: state.sessionId, text: retryText },
  });
  await handleCommandResponse(retryRes, retryText);
  return true;
}

async function cancelActiveDraft() {
  const draft = state.activeDraft;
  if (!draft) return { ok: false, error: "No active draft", draft: null };
  state.activeDraft = null;
  try {
    const res = await api("/confirm/cancel", {
      method: "POST",
      body: { session_id: state.sessionId, confirmation_id: draft.confirmationId },
    });
    return { ...res, draft };
  } catch (err) {
    return { ok: false, error: err.message, draft };
  }
}

function markDraftCardCancelled(cardEl, message) {
  if (!cardEl) return;
  const form = cardEl.querySelector(".pin-form, .normal-form");
  const notice = document.createElement("div");
  notice.className = "text-slate-400 text-xs font-medium";
  notice.textContent = message;
  if (form) form.replaceWith(notice);
  cardEl.querySelectorAll(".use-passkey-btn, .feedback, .save-contact-row").forEach((el) => el.remove());
}

composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = textInput.value.trim();
  if (!text || !state.sessionId) return;

  appendUserBubble(text);
  textInput.value = "";
  sendBtn.disabled = true;
  textInput.disabled = true; // not just the button -- Enter-to-submit can bypass a disabled submit button in some browsers

  try {
    if (!text.endsWith("?") && (await tryResolvePendingSlotFill(text))) {
      return;
    }

    const draft = state.activeDraft;
    const looksLikeQuestion = text.endsWith("?");

    // A pending card is open, and this message reads as being about it
    // (never on a question — "wait, what's my balance?" is still just a
    // question) rather than an unrelated command/chatbot message.
    if (draft && !looksLikeQuestion && DRAFT_CANCEL_RE.test(text) && !DRAFT_CORRECTION_RE.test(text)) {
      const cancelResult = await cancelActiveDraft();
      markDraftCardCancelled(draft.cardEl, "Cancelled");
      appendAssistantBubble(
        cancelResult.ok ? "Cancelled that payment." : "That payment wasn't still pending, so there was nothing to cancel."
      );
      return;
    }

    if (draft && !looksLikeQuestion && DRAFT_CORRECTION_RE.test(text)) {
      const strippedTarget = text
        .replace(DRAFT_LEADING_NO_RE, "")
        .replace(DRAFT_FILLER_WORDS_RE, " ")
        .replace(/\s+/g, " ")
        .trim();
      if (strippedTarget) {
        await cancelActiveDraft();
        markDraftCardCancelled(draft.cardEl, "Cancelled — redirected");
        const retryText = `Pay ${draft.amount} to ${strippedTarget}`;
        const retryRes = await api("/command/text", {
          method: "POST",
          body: { session_id: state.sessionId, text: retryText },
        });
        await handleCommandResponse(retryRes, retryText);
        return;
      }
      // Correction language with no extractable new target ("wait, let me
      // think") — nothing concrete to redirect to, so leave the draft open
      // and fall through to normal routing below rather than guessing.
    }

    if (SERVICE_HINT_RE.test(text)) {
      const svc = await api("/services/command", {
        method: "POST",
        body: { session_id: state.sessionId, text },
      });
      if (svc.ok) {
        await handleServiceCommandResponse(svc);
        return;
      }
    }

    if (/\bbpay\b|\bbiller\s*code\b/i.test(text)) {
      const res = await api("/bpay/command", {
        method: "POST",
        body: { session_id: state.sessionId, text },
      });
      await handleBpayCommandResponse(res, text);
    } else {
      const res = await api("/command/text", {
        method: "POST",
        body: { session_id: state.sessionId, text },
      });
      await handleCommandResponse(res, text);
    }
  } catch (err) {
    appendErrorBubble(`Request failed: ${err.message}`);
  } finally {
    sendBtn.disabled = false;
    textInput.disabled = false;
    focusPendingCardOrComposer();
    refreshAuditIfOpen();
  }
});

// After a reply, keyboard users go straight to a waiting confirmation field; otherwise back to the composer.
function focusPendingCardOrComposer() {
  const fields = feed.querySelectorAll(".normal-form input, .pin-box[data-idx='0']");
  if (fields.length) fields[fields.length - 1].focus();
  else textInput.focus();
}

// --- Service provider flow (tried first when the text names a bill or provider) ---

const SERVICE_HINT_RE = /\b(bill|invoice|top[- ]?up|telstra|agl|opal|sydney water|dr smith)\b/i;

// Colour per payment rail: emerald = peer to peer, indigo = service provider, amber = BPAY.
// Both the left edge (border) and the card's own background are tinted — a
// border alone is a thin 4px strip that's easy to miss, especially against
// the light theme's near-white default card background.
const RAIL_STYLE = {
  payid: { label: "Peer to peer", border: "border-emerald-500", text: "text-emerald-300", bg: "bg-emerald-950" },
  service: { label: "Service provider", border: "border-indigo-500", text: "text-indigo-300", bg: "bg-indigo-950" },
  bpay: { label: "BPAY", border: "border-amber-500", text: "text-amber-300", bg: "bg-amber-950" },
};

async function handleServiceCommandResponse(res) {
  appendAssistantBubble(res.read_back, { sensitive: true });
  await renderConfirmationCard(
    {
      intent: { amount: res.invoice.amount_cents / 100, currency: res.invoice.currency, payee_name: res.invoice.provider_name, note: null },
      txn_id: res.txn_id,
      confirmation: res.confirmation,
      payee: null,
    },
    { executeEndpoint: "/services/execute", rail: "service" }
  );
}

// --- BPAY command flow (routed above whenever the text mentions "BPAY") ---------

async function handleBpayCommandResponse(res, originalText) {
  if (!res.ok) {
    if (res.decision && res.decision.decision === "CLARIFY") {
      const p = res.parsed || {};
      appendErrorBubble(
        `${res.decision.reason} (so far: ${p.biller_display_name || p.biller_name || "biller?"}, ` +
          `CRN ${p.crn || "?"}, amount ${p.amount != null ? p.amount : "?"})`
      );
    } else {
      appendErrorBubble(res.error || "Could not understand that BPAY command.");
    }
    return;
  }
  appendAssistantBubble(res.read_back, { sensitive: true });
  await renderConfirmationCard(
    {
      intent: { amount: res.parsed.amount, currency: "AUD", payee_name: res.biller.biller_name, note: null },
      txn_id: res.txn_id,
      confirmation: res.confirmation,
      payee: null,
    },
    { executeEndpoint: "/bpay/execute", rail: "bpay" }
  );
}

// A bare "<verb> <one word>" with nothing else — e.g. "pay alice" or
// "send 20" — is always an incomplete payment attempt, never a sensible
// chatbot question, and the chatbot has no tools to complete a payment
// anyway. Deliberately narrow (exactly one trailing token) so it doesn't
// swallow idiomatic/informational phrasing that happens to start with the
// same verb ("pay attention to this", "send me my last transactions" both
// have 2+ trailing words and correctly fall through to the chatbot below).
const VERB_PLUS_BARE_NAME_RE = /^(pay|send|transfer)\s+([a-zA-Z][a-zA-Z'-]*)\s*$/i;
const VERB_PLUS_BARE_AMOUNT_RE = /^(pay|send|transfer)\s+(\d+(?:\.\d+)?)\s*$/i;
// Just the verb, nothing else at all ("pay", "send") -- even more clearly
// an abandoned payment attempt than the two above, since there's literally
// nothing else in the message for the chatbot to respond to meaningfully.
const BARE_VERB_ONLY_RE = /^(pay|send|transfer)\s*$/i;

async function handleCommandResponse(res, originalText) {
  if (!res.ok) {
    if (res.decision) {
      // A recognized payment command that was policy CLARIFY/BLOCK'd — not a
      // parse failure, so this stays as-is rather than going to support chat.
      if (res.decision.decision === "CLARIFY" && res.candidate_payees && res.candidate_payees.length) {
        // Let the user just click the contact they meant, instead of having
        // to retype the full command with a disambiguating PayID.
        renderClarifyPickerCard(res);
      } else {
        appendErrorBubble(`${res.decision.decision}: ${res.decision.reason}`);
      }
      return;
    }

    const bareVerbMatch = BARE_VERB_ONLY_RE.exec(originalText);
    const bareNameMatch = VERB_PLUS_BARE_NAME_RE.exec(originalText);
    const bareAmountMatch = VERB_PLUS_BARE_AMOUNT_RE.exec(originalText);
    if (bareVerbMatch) {
      state.pendingSlotFill = { type: "missing_both" };
      appendErrorBubble(`Who would you like to pay, and how much? Try: "Pay 12 to John".`);
    } else if (bareNameMatch) {
      state.pendingSlotFill = { type: "missing_amount", target: bareNameMatch[2] };
      appendErrorBubble(
        `How much would you like to pay ${bareNameMatch[2]}? Try: "Pay 12 to ${bareNameMatch[2]}", or just reply with the amount.`
      );
    } else if (bareAmountMatch) {
      state.pendingSlotFill = { type: "missing_target", amount: bareAmountMatch[2] };
      appendErrorBubble(
        `Who would you like to send ${bareAmountMatch[2]} to? Try: "Pay ${bareAmountMatch[2]} to John", or just reply with their name.`
      );
    } else {
      // Didn't parse as a payment command at all, and doesn't look like an
      // incomplete one either — hand it to the support chatbot instead of
      // just showing a raw parser error. It remembers past conversations
      // and can look up real contacts/transactions, but never moves money
      // itself.
      state.pendingSlotFill = null;
      await askSupportChat(originalText);
    }
    return;
  }

  appendAssistantBubble(res.read_back, { sensitive: true });
  await renderConfirmationCard(res);
}

function renderClarifyPickerCard(res) {
  const card = appendCard();
  const { amount, currency } = res.intent;
  card.innerHTML = `
    <div class="text-xs uppercase tracking-wide text-slate-400">Which one did you mean?</div>
    <div class="text-sm">${money(amount, currency)} to one of these:</div>
    <div class="flex flex-col gap-2 mt-1">
      ${res.candidate_payees
        .map(
          (c, i) => `
        <button type="button" class="clarify-pick-btn text-left bg-slate-900 hover:bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm transition" data-idx="${i}">
          <div class="font-medium">${escapeHtml(c.nickname)}</div>
          ${c.phone_number ? `<div class="text-[11px] text-slate-400 font-mono">PayID: ${escapeHtml(c.phone_number)}</div>` : ""}
        </button>`
        )
        .join("")}
    </div>
  `;

  card.querySelectorAll(".clarify-pick-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      card.querySelectorAll(".clarify-pick-btn").forEach((b) => (b.disabled = true));
      const chosen = res.candidate_payees[Number(btn.dataset.idx)];
      const target = chosen.phone_number || chosen.nickname;
      const retryText = `Pay ${amount} to ${target}`;
      appendUserBubble(chosen.nickname);
      card.remove();
      try {
        const retryRes = await api("/command/text", {
          method: "POST",
          body: { session_id: state.sessionId, text: retryText },
        });
        await handleCommandResponse(retryRes, retryText);
      } catch (err) {
        appendErrorBubble(`Request failed: ${err.message}`);
      } finally {
        refreshAuditIfOpen();
      }
    });
  });
}

async function askSupportChat(message) {
  const thinking = appendAssistantBubble("Thinking… (this runs on your local model and can take a minute or more on CPU)");
  const controller = new AbortController();
  const timeoutMs = 120000;
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await api("/support/chat", {
      method: "POST",
      body: { session_id: state.sessionId, message },
      signal: controller.signal,
    });
    if (res.ok) {
      thinking.querySelector("div").textContent = res.reply;
    } else {
      thinking.remove();
      appendErrorBubble(res.error || "Support chat is temporarily unavailable.");
    }
  } catch (err) {
    thinking.remove();
    if (err.name === "AbortError") {
      appendErrorBubble(
        `Your local model took longer than ${Math.round(timeoutMs / 1000)}s to respond and was cancelled. ` +
          `A smaller/faster model, or running Ollama with GPU support, would speed this up.`
      );
    } else {
      appendErrorBubble(`Request failed: ${err.message}`);
    }
  } finally {
    clearTimeout(timeoutId);
  }
}

// --- Confirmation card (normal phrase or PIN) -----------------------------------

async function renderConfirmationCard(cmdRes, opts = {}) {
  const { intent, txn_id, confirmation, payee } = cmdRes;
  const rail = opts.rail || "payid";

  // A payment started from the general view still belongs in that
  // contact's (or BPAY biller's) own thread, same as paying someone from
  // a phone's main dial pad still lands in their message thread -- not
  // just wherever the composer happened to be.
  let draftThreadKey = null;
  if (window.VPOffline) {
    if (payee) draftThreadKey = window.VPOffline.payeeThreadKey(payee.payee_id);
    else if (rail === "bpay" && cmdRes.biller) draftThreadKey = window.VPOffline.billerThreadKey(cmdRes.biller.biller_code);
  }

  // The global shortcut from the original Private Mode spec: paying into a
  // hidden thread interrupts with a biometric/PIN prompt *before* any
  // payment detail is shown, when the command came from somewhere else
  // (General, or another thread). Already inside that hidden thread, no
  // re-prompt -- reaching it at all already went through the Private
  // section's own authenticateBiometric() check.
  if (draftThreadKey && draftThreadKey !== state.activeThreadKey && (await window.VPOffline.isThreadHidden(draftThreadKey))) {
    const ok = await VocalPrivacy.authenticateBiometric(`Pay ${intent.payee_name}`);
    if (!ok) {
      appendErrorBubble("Authentication required to pay a private contact.");
      return;
    }
  }

  const card = appendCard();
  const required = confirmation.required_confirmation;
  const executeEndpoint = opts.executeEndpoint || "/pay/execute";
  const railStyle = RAIL_STYLE[rail] || RAIL_STYLE.payid;
  card.classList.remove("bg-slate-800");
  card.classList.add("border-l-4", railStyle.border, railStyle.bg);

  state.activeDraft = {
    confirmationId: confirmation.confirmation_id,
    txnId: txn_id,
    amount: intent.amount,
    currency: intent.currency,
    cardEl: card,
  };

  // card.dataset.threadKey (rather than state.activeDraft, which is cleared
  // once confirmed) outlives confirmation/execution so later outcomes can
  // still mirror into the right thread -- see mirrorBubble().
  if (draftThreadKey) {
    card.dataset.threadKey = draftThreadKey;
    if (draftThreadKey !== state.activeThreadKey && cmdRes.read_back) {
      window.VPOffline.recordMessage(draftThreadKey, "assistant", cmdRes.read_back).catch(() => {});
    }
  }

  const header = `
    <div class="flex items-center justify-between">
      <span class="text-xs uppercase tracking-wide ${railStyle.text}">${railStyle.label}</span>
      <span class="text-[11px] px-2 py-0.5 rounded-full ${required === "pin" ? "bg-amber-500/20 text-amber-300" : "bg-slate-700 text-slate-300"}">
        ${required === "pin" ? "PIN required" : "Type CONFIRM"}
      </span>
    </div>
    <div class="text-lg font-semibold">${money(intent.amount, intent.currency)} → ${VocalPrivacy.sens(escapeHtml(intent.payee_name))}</div>
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
    wirePinForm(card, txn_id, confirmation.confirmation_id, executeEndpoint);
  } else {
    card.innerHTML = header + normalFormHtml();
    wireNormalForm(card, txn_id, confirmation.confirmation_id, executeEndpoint);
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
    <button type="button" class="use-passkey-btn text-[11px] text-sky-400 hover:text-sky-300 underline underline-offset-2">
      Use a passkey instead (fingerprint / face / Windows Hello)
    </button>
    <p class="feedback text-xs h-4"></p>
  `;
}

function wireNormalForm(card, txnId, confirmationId, executeEndpoint) {
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
      await handleConfirmResult(card, form, feedback, res, txnId, executeEndpoint);
    } catch (err) {
      feedback.textContent = `Request failed: ${err.message}`;
      feedback.className = "feedback text-xs h-4 text-rose-400";
    } finally {
      setFormBusy(form, false);
      refreshAuditIfOpen();
    }
  });
}

function wirePinForm(card, txnId, confirmationId, executeEndpoint) {
  const form = card.querySelector(".pin-form");
  const boxes = [...form.querySelectorAll(".pin-box")];
  const feedback = card.querySelector(".feedback");
  const passkeyBtn = card.querySelector(".use-passkey-btn");
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
      await handleConfirmResult(card, form, feedback, res, txnId, executeEndpoint);
    } catch (err) {
      feedback.textContent = `Request failed: ${err.message}`;
      feedback.className = "feedback text-xs h-4 text-rose-400";
    } finally {
      setFormBusy(form, false);
      refreshAuditIfOpen();
    }
  });

  if (passkeyBtn && window.PublicKeyCredential) {
    passkeyBtn.addEventListener("click", async () => {
      passkeyBtn.disabled = true;
      feedback.textContent = "Waiting for your passkey (Touch ID / Face ID / Windows Hello)…";
      feedback.className = "feedback text-xs h-4 text-slate-400";
      try {
        const res = await authenticateWithPasskey(confirmationId);
        await handleConfirmResult(card, form, feedback, res, txnId, executeEndpoint);
      } catch (err) {
        feedback.textContent = `Passkey failed: ${err.message}`;
        feedback.className = "feedback text-xs h-4 text-rose-400";
      } finally {
        passkeyBtn.disabled = false;
      }
    });
  } else if (passkeyBtn) {
    passkeyBtn.disabled = true;
    passkeyBtn.title = "Passkeys aren't supported in this browser";
  }
}

function setFormBusy(form, busy) {
  [...form.elements].forEach((el) => (el.disabled = busy));
}

async function handleConfirmResult(card, form, feedback, res, txnId, executeEndpoint) {
  if (!res.ok) {
    feedback.textContent = res.error || "Confirmation failed.";
    feedback.className = "feedback text-xs h-4 text-rose-400";
    VocalPrivacy.speak(res.error || "Confirmation failed.");
    if (res.error && res.error.includes("Too many attempts")) {
      setFormBusy(form, true);
    }
    return;
  }

  feedback.textContent = "";
  const confirmedNotice = document.createElement("div");
  confirmedNotice.className = "text-emerald-400 text-xs font-medium flex items-center gap-1.5";
  confirmedNotice.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4"><path d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4Z"/></svg>Confirmed`;
  form.replaceWith(confirmedNotice);
  VocalPrivacy.speak("Confirmed. Ready to pay.");
  recordBubble("assistant", "Confirmed. Ready to pay.");
  mirrorBubble(card, "assistant", "Confirmed. Ready to pay.");
  card.querySelectorAll(".use-passkey-btn").forEach((el) => el.remove());

  // Once confirmed, a correction/cancel typed in the composer no longer has
  // anything pending to act on for this card.
  if (state.activeDraft && state.activeDraft.txnId === txnId) {
    state.activeDraft = null;
  }

  const payBtn = document.createElement("button");
  payBtn.className = "w-full bg-emerald-500 hover:bg-emerald-400 text-slate-950 text-sm font-semibold py-2.5 rounded-lg transition";
  payBtn.textContent = "Pay now";
  payBtn.addEventListener("click", () => executePayment(card, payBtn, txnId, executeEndpoint));
  card.appendChild(payBtn);
}

// --- Payment execution -------------------------------------------------------

async function executePayment(card, payBtn, txnId, executeEndpoint) {
  const endpoint = executeEndpoint || "/pay/execute";
  payBtn.disabled = true;
  payBtn.innerHTML = `<span class="inline-flex items-center gap-2 justify-center w-full">
    <span class="spinner"></span> ${endpoint === "/bpay/execute" ? "Settling via BPAY…" : endpoint === "/services/execute" ? "Settling with provider…" : "Authorizing through Stripe…"}
  </span>`;

  try {
    const res = await api(endpoint, {
      method: "POST",
      body: { session_id: state.sessionId, txn_id: txnId },
    });

    if (res.ok && (endpoint === "/bpay/execute" || endpoint === "/services/execute")) {
      const railLabel = endpoint === "/bpay/execute" ? "BPAY" : "Service provider";
      payBtn.outerHTML = `
        <div class="bg-emerald-950 border border-emerald-800 rounded-lg px-3 py-2.5 text-sm space-y-1">
          <div class="text-emerald-300 font-medium">${railLabel} payment ${escapeHtml(res.final_status)}</div>
          <div class="text-[11px] text-emerald-300 font-mono">${escapeHtml(res.simulated_reference)}</div>
          <div class="text-[10px] text-slate-400">${escapeHtml(res.note)}</div>
        </div>`;
      VocalPrivacy.speak(`${railLabel} payment ${res.final_status}.`);
      recordBubble("assistant", `${railLabel} payment ${res.final_status}. Reference ${res.simulated_reference}.`);
      mirrorBubble(card, "assistant", `${railLabel} payment ${res.final_status}. Reference ${res.simulated_reference}.`);
    } else if (res.ok) {
      payBtn.outerHTML = `
        <div class="bg-emerald-950 border border-emerald-800 rounded-lg px-3 py-2.5 text-sm space-y-2">
          <div>
            <div class="text-emerald-300 font-medium">Payment ${escapeHtml(res.final_status)}</div>
            <div class="text-[11px] text-emerald-300 font-mono mt-0.5">${escapeHtml(res.stripe_payment_intent_id)}</div>
          </div>
          ${renderReceiverEvidence(res.receiver_evidence)}
        </div>`;
      VocalPrivacy.speak(`Payment ${res.final_status}.`);
      recordBubble("assistant", `Payment ${res.final_status}. ${res.stripe_payment_intent_id}`);
      mirrorBubble(card, "assistant", `Payment ${res.final_status}. ${res.stripe_payment_intent_id}`);
    } else {
      payBtn.outerHTML = `
        <div class="bg-rose-950 border border-rose-800 rounded-lg px-3 py-2.5 text-sm">
          <div class="text-rose-300 font-medium">${escapeHtml(res.error || "Payment failed")}</div>
          ${res.details ? `<div class="text-[11px] text-rose-400/80 mt-0.5">${escapeHtml(res.details)}</div>` : ""}
        </div>`;
      VocalPrivacy.speak(res.error || "Payment failed.");
      recordBubble("error", res.error || "Payment failed.");
      mirrorBubble(card, "error", res.error || "Payment failed.");
    }
  } catch (err) {
    payBtn.outerHTML = `<div class="bg-rose-950 border border-rose-800 rounded-lg px-3 py-2.5 text-sm text-rose-300">Request failed: ${escapeHtml(err.message)}</div>`;
    VocalPrivacy.speak("Payment request failed.");
    recordBubble("error", `Request failed: ${err.message}`);
    mirrorBubble(card, "error", `Request failed: ${err.message}`);
  } finally {
    refreshAuditIfOpen();
  }
}

function renderReceiverEvidence(ev) {
  if (!ev) {
    return `<div class="text-[11px] text-slate-400 border-t border-emerald-900 pt-2">
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
        <div class="text-[11px] text-emerald-300 mt-1">
          ${amount} now in their Stripe balance (their account, not the platform's).
        </div>
        <div class="text-[10px] text-slate-400 font-mono mt-1 truncate" title="${escapeHtml(ev.destination_account_id)}">
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

const drawerBackdrop = document.getElementById("drawer-backdrop");
const srAnnounce = document.getElementById("sr-announce");
const SR_KEY = "vocalpay.srAnnounce";
let lastFocusedBeforeDrawer = null;

function applyScreenReaderSetting(on) {
  feed.setAttribute("aria-live", on ? "polite" : "off");
  srAnnounce.checked = on;
}

try {
  applyScreenReaderSetting(localStorage.getItem(SR_KEY) === "1");
} catch (err) {
  applyScreenReaderSetting(false);
}

srAnnounce.addEventListener("change", () => {
  applyScreenReaderSetting(srAnnounce.checked);
  try {
    localStorage.setItem(SR_KEY, srAnnounce.checked ? "1" : "0");
  } catch (err) {
    // Preference is per device; if storage is blocked it simply lasts for this page.
  }
});

function openPanel(tab) {
  if (!state.panelOpen) lastFocusedBeforeDrawer = document.activeElement;
  state.panelOpen = true;
  state.activeTab = tab;
  sidePanel.classList.remove("translate-x-full");
  drawerBackdrop.classList.remove("hidden");
  panelSetup.classList.toggle("hidden", tab !== "setup");
  panelSetup.classList.toggle("flex", tab === "setup");
  panelAudit.classList.toggle("hidden", tab !== "audit");
  panelAudit.classList.toggle("flex", tab === "audit");
  setupToggle.classList.toggle("bg-slate-800", tab === "setup");
  auditToggle.classList.toggle("bg-slate-800", tab === "audit");
  if (tab === "setup") loadSetupData();
  if (tab === "audit") refreshAudit();
  sidePanel.focus();
}

function closePanel() {
  state.panelOpen = false;
  sidePanel.classList.add("translate-x-full");
  drawerBackdrop.classList.add("hidden");
  setupToggle.classList.remove("bg-slate-800");
  auditToggle.classList.remove("bg-slate-800");
  if (lastFocusedBeforeDrawer && document.contains(lastFocusedBeforeDrawer)) {
    lastFocusedBeforeDrawer.focus();
  }
}

drawerBackdrop.addEventListener("click", closePanel);
document.querySelectorAll(".drawer-close").forEach((btn) => btn.addEventListener("click", closePanel));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.panelOpen) closePanel();
});

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
  await Promise.all([loadPayees(), loadPaymentMethods(), loadPasskeys(), loadBpayBillers()]);
}

async function loadPasskeys() {
  try {
    const res = await api("/webauthn/credentials");
    renderPasskeyList(res.credentials);
  } catch (err) {
    passkeyList.innerHTML = `<p class="text-rose-400 text-xs">Failed to load: ${escapeHtml(err.message)}</p>`;
  }
}

function renderPasskeyList(credentials) {
  if (!credentials.length) {
    passkeyList.innerHTML = `<p class="text-slate-400 text-xs">No passkeys yet — PIN step-ups still work either way.</p>`;
    return;
  }
  passkeyList.innerHTML = credentials
    .map(
      (c) => `
      <div class="bg-slate-800/60 border border-slate-800 rounded-lg px-3 py-2 text-xs flex items-center justify-between">
        <span>${escapeHtml(c.label || "Passkey")}</span>
        <span class="text-slate-400 font-mono text-[10px]">${escapeHtml(c.credential_id.slice(0, 10))}…</span>
      </div>`
    )
    .join("");
}

// Android's WebView (what the offline app runs in) typically has no
// window.PublicKeyCredential at all, so the button below was getting
// disabled with only a `title` tooltip as explanation -- invisible on a
// touchscreen, since tapping a disabled button does nothing and there's no
// hover to show a tooltip. Passkeys also can't work offline regardless (no
// server to issue/verify a WebAuthn challenge), so in the offline app this
// is shown as visible, static feedback instead of a silently-dead button.
if (window.VPOffline) {
  registerPasskeyBtn.disabled = true;
  registerPasskeyBtn.title = "Passkeys aren't available in the offline app";
  passkeyFeedback.textContent = "Passkeys aren't available offline — PIN step-ups still work for larger payments.";
  passkeyFeedback.className = "text-[11px] h-4 mt-1 text-slate-400";
} else if (!window.PublicKeyCredential) {
  registerPasskeyBtn.disabled = true;
  registerPasskeyBtn.title = "Passkeys aren't supported in this browser";
}

registerPasskeyBtn.addEventListener("click", async () => {
  registerPasskeyBtn.disabled = true;
  passkeyFeedback.textContent = "Follow your browser/device prompt…";
  passkeyFeedback.className = "text-[11px] h-4 mt-1 text-slate-400";
  try {
    const res = await registerPasskey("This device");
    if (res.ok) {
      passkeyFeedback.textContent = "Passkey registered.";
      passkeyFeedback.className = "text-[11px] h-4 mt-1 text-emerald-400";
      loadPasskeys();
    } else {
      passkeyFeedback.textContent = res.error || "Failed to register passkey.";
      passkeyFeedback.className = "text-[11px] h-4 mt-1 text-rose-400";
    }
  } catch (err) {
    passkeyFeedback.textContent = `Failed: ${err.message}`;
    passkeyFeedback.className = "text-[11px] h-4 mt-1 text-rose-400";
  } finally {
    registerPasskeyBtn.disabled = false;
  }
});

async function loadBpayBillers() {
  try {
    const res = await api("/bpay/billers");
    renderBpayBillerList(res.billers);
  } catch (err) {
    bpayBillerList.innerHTML = `<p class="text-rose-400 text-xs">Failed to load: ${escapeHtml(err.message)}</p>`;
  }
}

function renderBpayBillerList(billers) {
  if (!billers.length) {
    bpayBillerList.innerHTML = `<p class="text-slate-400 text-xs">No saved billers yet.</p>`;
    return;
  }
  bpayBillerList.innerHTML = billers
    .map(
      (b) => `
      <div class="bg-slate-800/60 border border-slate-800 rounded-lg px-3 py-2 text-xs">
        <div class="font-medium">${escapeHtml(b.nickname)}</div>
        <div class="text-slate-400 font-mono mt-0.5">Code ${escapeHtml(b.biller_code)} · CRN ${escapeHtml(b.crn)}</div>
      </div>`
    )
    .join("");
}

async function loadPayees() {
  try {
    const res = await api("/payees");
    renderPayeeList(res.payees);
    // The Setup drawer is an overlay, not a navigation -- a contact added
    // or imported there wouldn't otherwise show up as a thread until some
    // unrelated screen change happened to re-render the list.
    if (window.VPOffline && !threadListView.classList.contains("hidden")) renderThreadList();
  } catch (err) {
    payeeList.innerHTML = `<p class="text-rose-400 text-xs">Failed to load: ${escapeHtml(err.message)}</p>`;
  }
}

function renderPayeeList(payees) {
  if (!payees.length) {
    payeeList.innerHTML = `<p class="text-slate-400 text-xs">No contacts yet — messages to a new name will require a PIN.</p>`;
    return;
  }
  payeeList.innerHTML = payees
    .map((p) => {
      const tracked = Boolean(p.stripe_connected_account_id);
      return `
      <div class="payee-card bg-slate-800/60 border border-slate-800 rounded-lg px-3 py-2 text-xs" data-payee-id="${p.payee_id}">
        <div class="flex items-center justify-between">
          <span class="font-medium">${escapeHtml(p.nickname)}</span>
          <span class="${tracked ? "text-emerald-400" : "text-slate-400"} text-[10px] font-semibold uppercase">
            ${tracked ? "receiver tracked" : escapeHtml(p.type)}
          </span>
        </div>
        ${p.linked_contact_id ? `<div class="text-sky-400/80 mt-0.5">Same person as another saved number</div>` : ""}
        <ul class="payid-list mt-1 space-y-0.5 text-slate-400 font-mono"></ul>
        <form class="payid-add-form mt-1.5 flex items-center gap-1.5">
          <label class="sr-only" for="payid-input-${p.payee_id}">Add a PayID for ${escapeHtml(p.nickname)}</label>
          <input id="payid-input-${p.payee_id}" type="text" placeholder="Another mobile, email or ABN"
            class="flex-1 min-w-0 bg-slate-900 border border-slate-700 rounded-md px-2 py-1 text-[11px] outline-none focus:ring-2 focus:ring-emerald-500" />
          <button type="submit" class="text-[11px] px-2 py-1 rounded-md border border-slate-700 hover:bg-slate-800 whitespace-nowrap">Add PayID</button>
        </form>
        <p class="payid-feedback text-[11px] h-4 mt-0.5"></p>
        ${tracked ? `<button class="check-balance-btn text-emerald-400 hover:text-emerald-300 mt-1 underline underline-offset-2" data-payee-id="${p.payee_id}">Check receiver balance</button>
        <div class="balance-result text-slate-400 mt-1"></div>` : ""}
      </div>`;
    })
    .join("");

  payeeList.querySelectorAll(".check-balance-btn").forEach((btn) => {
    btn.addEventListener("click", () => checkReceiverBalance(btn));
  });
  payeeList.querySelectorAll(".payee-card").forEach((card) => {
    loadPayIdsInto(card);
    card.querySelector(".payid-add-form").addEventListener("submit", (e) => {
      e.preventDefault();
      addPayIdFromCard(card);
    });
  });
}

async function loadPayIdsInto(card) {
  const list = card.querySelector(".payid-list");
  try {
    const res = await api(`/payees/${card.dataset.payeeId}/payids`);
    list.innerHTML = res.payids
      .map(
        (p) => `<li>${escapeHtml(p.pay_id_type)}: ${escapeHtml(p.value_normalized)}${p.is_primary ? ' <span class="text-emerald-400 not-italic font-sans">(primary)</span>' : ""}</li>`
      )
      .join("");
  } catch (err) {
    list.innerHTML = `<li class="text-rose-400">Could not load PayIDs: ${escapeHtml(err.message)}</li>`;
  }
}

async function addPayIdFromCard(card) {
  const input = card.querySelector(".payid-add-form input");
  const feedback = card.querySelector(".payid-feedback");
  const value = input.value.trim();
  if (!value) return;
  try {
    const res = await api(`/payees/${card.dataset.payeeId}/payids`, { method: "POST", body: { value } });
    if (res.ok) {
      feedback.textContent = `Added ${res.pay_id_type} PayID.`;
      feedback.className = "payid-feedback text-[11px] h-4 mt-0.5 text-emerald-400";
      input.value = "";
      loadPayIdsInto(card);
    } else {
      feedback.textContent = res.error || "Could not add that PayID.";
      feedback.className = "payid-feedback text-[11px] h-4 mt-0.5 text-rose-400";
    }
  } catch (err) {
    feedback.textContent = `Request failed: ${err.message}`;
    feedback.className = "payid-feedback text-[11px] h-4 mt-0.5 text-rose-400";
  }
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

// Reads the phone's own address book (via the native Contacts plugin — see
// @capacitor-community/contacts) and offers each entry with an Australian
// mobile number as a payee. Nothing here ever leaves the device: it's a
// local plugin call, same as the rest of the offline app. Entries without a
// usable 04xx mobile number are skipped (our PayID validator only accepts
// that format — see payid_validation.py / offline-engine.js classifyPayid),
// and a contact already saved under the same name is skipped too.
const importDeviceContactsBtn = document.getElementById("import-device-contacts-btn");
const importDeviceContactsFeedback = document.getElementById("import-device-contacts-feedback");
if (importDeviceContactsBtn) {
  importDeviceContactsBtn.addEventListener("click", async () => {
    const Contacts = window.Capacitor?.Plugins?.Contacts;
    if (!Contacts) {
      importDeviceContactsFeedback.textContent = "Contacts aren't available outside the installed app.";
      importDeviceContactsFeedback.className = "text-[11px] h-4 mt-1 text-rose-400";
      return;
    }
    importDeviceContactsBtn.disabled = true;
    importDeviceContactsBtn.textContent = "Reading contacts…";
    importDeviceContactsFeedback.textContent = "";
    try {
      const perm = await Contacts.requestPermissions();
      if (perm.contacts !== "granted") {
        importDeviceContactsFeedback.textContent = "Contacts permission was declined.";
        importDeviceContactsFeedback.className = "text-[11px] h-4 mt-1 text-rose-400";
        return;
      }
      const { contacts } = await Contacts.getContacts({ projection: { name: true, phones: true } });
      let added = 0;
      let skipped = 0;
      for (const c of contacts) {
        const nickname = c.name?.display?.trim();
        const rawPhone = c.phones?.[0]?.number;
        if (!nickname || !rawPhone) { skipped++; continue; }
        if (!/^(?:\+?61|0)?4\d{8}$/.test(rawPhone.replace(/[\s\-()]/g, ""))) { skipped++; continue; }
        const res = await api("/payees/add_contact", { method: "POST", body: { nickname, phone_number: rawPhone } });
        if (res.ok) added++; else skipped++;
      }
      importDeviceContactsFeedback.textContent = `Imported ${added} contact${added === 1 ? "" : "s"} with an Australian mobile number${skipped ? ` (skipped ${skipped} without one, or already saved)` : ""}.`;
      importDeviceContactsFeedback.className = "text-[11px] h-4 mt-1 text-emerald-400";
      loadPayees();
    } catch (err) {
      importDeviceContactsFeedback.textContent = `Failed: ${err.message}`;
      importDeviceContactsFeedback.className = "text-[11px] h-4 mt-1 text-rose-400";
    } finally {
      importDeviceContactsBtn.disabled = false;
      importDeviceContactsBtn.textContent = "+ Import from phone contacts";
    }
  });
}

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
    pmList.innerHTML = `<p class="text-slate-400 text-xs">No payment methods yet — payments can't be executed until one is added.</p>`;
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
    auditEvents.innerHTML = `<p class="text-slate-400">No events yet.</p>`;
    return;
  }
  auditEvents.innerHTML = events
    .map((ev, i) => {
      const time = new Date(ev.ts).toLocaleTimeString();
      return `
        <div class="border border-slate-800 rounded-lg p-2.5 ${i === events.length - 1 ? "ring-1 ring-emerald-700/60" : ""}">
          <div class="flex items-center justify-between">
            <span class="font-medium text-slate-200">${escapeHtml(ev.event_type)}</span>
            <span class="text-slate-400">${time}</span>
          </div>
          <div class="font-mono text-[10px] text-slate-400 mt-1 truncate" title="${ev.event_hash}">
            #${ev.event_hash.slice(0, 12)}
          </div>
        </div>`;
    })
    .join("");
  auditEvents.scrollTop = auditEvents.scrollHeight;
}

// --- Voice input (Whisper in the browser, Web Speech fallback; see voice.js) ----

window.VocalVoice.create({
  micBtn,
  micStatus,
  textInput,
  onState: (on) => setListening(on),
  // Anything started by voice is treated as a public-space payment: hide details until revealed.
  onResult: () => VocalPrivacy.set("privateMode", true),
});

// --- Privacy, spoken replies and display settings (see privacy.js) ------------

const privateToggle = document.getElementById("private-toggle");
privateToggle.addEventListener("click", () => VocalPrivacy.set("privateMode", !VocalPrivacy.get().privateMode));

const speakRepliesInput = document.getElementById("speak-replies");
const highContrastInput = document.getElementById("high-contrast");
const darkThemeInput = document.getElementById("dark-theme");
const textSizeSelect = document.getElementById("text-size");

function syncDisplaySettings() {
  const p = VocalPrivacy.get();
  speakRepliesInput.checked = p.speak;
  highContrastInput.checked = p.highContrast;
  darkThemeInput.checked = p.dark;
  textSizeSelect.value = p.textSize;
}
speakRepliesInput.addEventListener("change", () => VocalPrivacy.set("speak", speakRepliesInput.checked));
highContrastInput.addEventListener("change", () => VocalPrivacy.set("highContrast", highContrastInput.checked));
darkThemeInput.addEventListener("change", () => VocalPrivacy.set("dark", darkThemeInput.checked));
textSizeSelect.addEventListener("change", () => VocalPrivacy.set("textSize", textSizeSelect.value));
syncDisplaySettings();

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
