import ast
import logging
import os
import re

import psycopg
from psycopg.rows import dict_row
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.messages.utils import trim_messages
from langchain_core.tools import tool
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

import db
from db import get_conn
from payees_repo import find_payees_by_name, get_payee
from transactions_repo import list_recent_transactions, get_transaction
from stripe_service import get_account_balance

logger = logging.getLogger("vocalpay.support_chat")

# Self-hosted only, deliberately -- no cloud LLM API (Gemini, OpenAI, etc.)
# anywhere in this app. Needs a real Ollama server reachable at
# OLLAMA_BASE_URL; no API key involved.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
# Bounds how long a single Ollama call can run server-side. /support/chat
# runs as a plain sync endpoint (FastAPI's sync thread pool), so a hung call
# without this would tie up a worker thread indefinitely -- enough of those
# stacking up could stall unrelated endpoints sharing the same pool. The
# frontend's own ~120s AbortController timeout is a backstop for this, not
# the primary defense.
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "90"))

# Prompt window: the checkpointer keeps the full thread, but only the most recent
# messages are sent to the model on each turn.
MEMORY_WINDOW_MESSAGES = int(os.getenv("MEMORY_WINDOW_MESSAGES", "20"))

# ---------------------------------------------------------------------------
# Multi-agent support chat.
#
# One general-purpose agent with five tools bolted on (the previous design)
# works, but every turn pays for the model holding all five tools' worth of
# descriptions in context and guessing which ones apply, even for a question
# that's obviously about one narrow domain. Splitting into specialists with
# their own short, focused system prompt and only the tools relevant to their
# domain means each one plays to its strength instead of hedging across all
# of them, and keeps each individual prompt/tool-list small on an 8B local
# model where every token of context costs real latency.
#
# Routing: a deterministic keyword pass first (ROUTE_KEYWORDS below) catches
# the common, unambiguous phrasing for each domain with zero extra model
# call. Only a message that matches no domain's keywords falls back to one
# short LLM classification call (_llm_route) -- genuine reasoning for
# ambiguous phrasing, without paying that extra round-trip on every turn.
# That fallback call's result is never written into the persisted
# conversation (it's a plain function return value, not a graph state
# update), so it can't pollute the chat history the user/specialist sees.
# ---------------------------------------------------------------------------

ROUTE_KEYWORDS = {
    "account": (
        "contact", "contacts", "payid", "pay id", "policy", "rule", "rules",
        "limit", "cap", "pin", "confirm", "how does", "how do i", "what happens if",
        "recurring", "scheduled payment", "schedule", "every friday", "every week", "every month",
    ),
    "transactions": (
        "transaction", "transactions", "payment history", "last payment", "last transfer",
        "balance", "receipt", "txn", "status of", "did it go through", "did it arrive",
        "receiver", "received",
    ),
    "bills": (
        "bpay", "biller", "bill", "bills", "invoice", "invoices", "provider", "providers",
        "telstra", "agl", "sydney water", "dr smith", "opal", "utility", "telco",
    ),
}
ROUTE_NAMES = ("account", "transactions", "bills", "general")

ACCOUNT_SYSTEM_PROMPT = (
    "You are VocalPay's account specialist. You know the user's saved contacts, the app's payment "
    "rules (what gets blocked, what needs a PIN, the amount cap), and their recurring/scheduled "
    "payments -- answer those questions using your tools, never guessing or inventing a name, PayID, or "
    "schedule. You cannot move money yourself, and you cannot create a recurring schedule either; if "
    "asked to pay, send money, or set up a recurring payment, tell the user to say it directly in the "
    "chat (e.g. 'Pay 12 to John', or 'Pay Sam 20 every Friday' to schedule one) -- real transfers and new "
    "schedules must go through the app's own confirmation flow, which you don't have access to. Keep "
    "answers short and conversational."
)
TRANSACTIONS_SYSTEM_PROMPT = (
    "You are VocalPay's transactions specialist. You look up the user's payment history, a specific "
    "transaction's status, and a contact's real Stripe receiver balance (to confirm they actually got "
    "paid) -- using your tools, never guessing or inventing an amount, status, or date. You cannot move "
    "money yourself; if asked to pay or send money, tell the user to type a payment command directly in "
    "the chat (e.g. 'Pay 12 to John'). Keep answers short and conversational, and always state amounts "
    "with their currency."
)
BILLS_SYSTEM_PROMPT = (
    "You are VocalPay's bills specialist. You know the user's open and paid service-provider invoices "
    "(utilities, telco, health, transport), the registered BPAY billers, and the user's own saved BPAY "
    "billers -- using your tools, never guessing or inventing an amount, due date, or biller code. You "
    "cannot pay a bill yourself; if asked to pay one, tell the user to type it directly in the chat (e.g. "
    "'Pay the Telstra bill' or 'Pay Origin BPAY 30 dollars, biller code 111999, reference ...') -- real "
    "settlement always goes through the app's own confirmation flow. Keep answers short and conversational."
)
GENERAL_SYSTEM_PROMPT = (
    "You are VocalPay's support assistant. You don't have specific tools for this question, so answer "
    "briefly from general knowledge of how VocalPay works, or say you're not sure rather than guessing "
    "at the user's actual data (contacts, transactions, bills -- ask them to rephrase toward one of those "
    "topics and you'll look it up). You cannot move money yourself; real payments must be typed directly "
    "in the chat (e.g. 'Pay 12 to John') and go through the app's own confirmation/PIN flow. Keep answers "
    "short and conversational, like a real support agent, not a wall of text."
)

_graph = None
_graph_schema = None
_saver_conn = None


def _build_model():
    if LLM_PROVIDER != "ollama":
        raise RuntimeError(f"Unknown LLM_PROVIDER '{LLM_PROVIDER}' -- only 'ollama' is supported")

    from langchain_ollama import ChatOllama

    # No API key needed -- this calls a self-hosted Ollama server over plain
    # HTTP. If OLLAMA_BASE_URL is unreachable, this raises at first use (a
    # connection error), not here at construction time. client_kwargs is
    # passed straight to the underlying `ollama` package's Client, which
    # wraps httpx and honors `timeout` -- confirmed via
    # `ChatOllama(...)._client._client.timeout`.
    return ChatOllama(
        base_url=OLLAMA_BASE_URL,
        model=OLLAMA_MODEL,
        temperature=0.3,
        client_kwargs={"timeout": OLLAMA_TIMEOUT_SECONDS},
    )


def _checkpointer():
    global _saver_conn
    kwargs = {"autocommit": True, "row_factory": dict_row, "options": f"-c search_path={db.domain_schema('memory')}"}
    _saver_conn = psycopg.connect(db.DATABASE_URL, **kwargs)
    saver = PostgresSaver(_saver_conn)
    saver.setup()
    return saver


# --- Routing ------------------------------------------------------------------

def _keyword_route(message: str) -> str | None:
    lowered = message.lower()
    for route, keywords in ROUTE_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            return route
    return None


def _llm_route(message: str) -> str:
    """One short classification call, used only when no keyword matched. Its
    result is a plain return value, never appended to graph state, so it
    never shows up in the conversation the user or a specialist agent sees."""
    prompt = (
        "Classify the user's message into exactly one topic: account, transactions, bills, or general.\n"
        "- account: contacts, PayIDs, payment rules/limits/PIN\n"
        "- transactions: payment history, a transaction's status, a contact's receiver balance\n"
        "- bills: BPAY billers, service-provider bills/invoices\n"
        "- general: anything else, including greetings and small talk\n"
        "Reply with just the single word for the topic, nothing else.\n\n"
        f"Message: {message}"
    )
    try:
        reply = _build_model().invoke([HumanMessage(content=prompt)])
        text = _extract_text(reply.content).strip().lower()
    except Exception as e:
        logger.warning("support_chat: LLM route classification failed (%s), defaulting to general", e)
        return "general"
    for route in ROUTE_NAMES:
        if route in text:
            return route
    return "general"


def _route(state: MessagesState) -> str:
    message = state["messages"][-1].content if state["messages"] else ""
    route = _keyword_route(message) or _llm_route(message)
    logger.info("support_chat: routed to %s agent", route)
    return f"{route}_agent"


# --- Specialist subgraphs ------------------------------------------------------

def _add_specialist(builder: StateGraph, name: str, system_prompt: str, tools: list) -> None:
    """Wires one specialist's ReAct loop (its own system prompt + tool subset)
    into the shared graph: agent node -> tool node (if it used one) -> back to
    the agent, until it answers without calling a tool."""
    bound_model = _build_model().bind_tools(tools) if tools else _build_model()
    agent_node = f"{name}_agent"
    tools_node = f"{name}_tools"

    def call_model(state: MessagesState):
        window = trim_messages(
            state["messages"],
            max_tokens=MEMORY_WINDOW_MESSAGES,
            token_counter=len,
            strategy="last",
            start_on="human",
            include_system=False,
        )
        response = bound_model.invoke([SystemMessage(content=system_prompt)] + window)
        return {"messages": [response]}

    builder.add_node(agent_node, call_model)
    if tools:
        builder.add_node(tools_node, ToolNode(tools))
        builder.add_conditional_edges(agent_node, tools_condition, {"tools": tools_node, END: END})
        builder.add_edge(tools_node, agent_node)
    else:
        builder.add_edge(agent_node, END)


def _build_graph():
    builder = StateGraph(MessagesState)
    _add_specialist(builder, "account", ACCOUNT_SYSTEM_PROMPT, _account_tools())
    _add_specialist(builder, "transactions", TRANSACTIONS_SYSTEM_PROMPT, _transactions_tools())
    _add_specialist(builder, "bills", BILLS_SYSTEM_PROMPT, _bills_tools())
    _add_specialist(builder, "general", GENERAL_SYSTEM_PROMPT, [])
    builder.add_conditional_edges(
        START,
        _route,
        {"account_agent": "account_agent", "transactions_agent": "transactions_agent",
         "bills_agent": "bills_agent", "general_agent": "general_agent"},
    )
    return builder.compile(checkpointer=_checkpointer())


def _get_graph():
    global _graph, _graph_schema
    if _graph is None or _graph_schema != db.DB_SCHEMA:
        reset_graph()
        _graph = _build_graph()
        _graph_schema = db.DB_SCHEMA
    return _graph


def reset_graph() -> None:
    """Drops the cached graph and its Postgres connection; the next call rebuilds from the stored thread."""
    global _graph, _saver_conn
    if _saver_conn is not None:
        _saver_conn.close()
    _saver_conn = None
    _graph = None


# --- Tools, grouped by the specialist that owns them ---------------------------

def _account_tools():
    from users_repo import DEMO_USER_ID

    @tool
    def list_contacts() -> str:
        """List the user's saved contacts: name, PayID, and whether payments to them are receiver-tracked."""
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT nickname, phone_number, stripe_connected_account_id FROM payees "
                "WHERE user_id=? AND is_contact=1 ORDER BY created_at",
                (DEMO_USER_ID,),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return "No saved contacts yet."
        lines = []
        for r in rows:
            tracked = "receiver-tracked" if r["stripe_connected_account_id"] else "not receiver-tracked"
            lines.append(f"- {r['nickname']} (PayID: {r['phone_number'] or 'none'}, {tracked})")
        return "\n".join(lines)

    @tool
    def explain_payment_policy() -> str:
        """Explain VocalPay's payment rules: what gets blocked, what needs a PIN, and the amount cap."""
        return (
            "Payment rules: the payee must be a saved contact with a PayID (or a valid PayID looked up "
            "directly) -- an unknown name or number is blocked outright. Payments up to 50 AUD to a known "
            "contact only need typing CONFIRM. Payments over 50 AUD, or to a brand-new contact, require the "
            "4-digit PIN (demo PIN: 1234). A confirmation expires after 120 seconds, and 3 wrong PIN "
            "attempts locks that payment."
        )

    @tool
    def list_recurring_schedules() -> str:
        """List the user's recurring/scheduled payments: amount, cadence, next run date, and status."""
        from recurring_repo import list_schedules
        rows = list_schedules(DEMO_USER_ID)
        if not rows:
            return "No recurring payments scheduled."
        lines = []
        for r in rows:
            amount = r["amount_cents"] / 100
            target = get_payee(r["target_identifier"]) if r["payment_rail"] == "payid" else None
            target_label = target["nickname"] if target else r["target_identifier"]
            lines.append(
                f"- AUD {amount:.2f} to {target_label} ({r['payment_rail']}), {r['cadence']}, "
                f"next on {r['next_run_at'][:10]}, {r['status']} (schedule_id {r['schedule_id']})"
            )
        return "\n".join(lines)

    return [list_contacts, explain_payment_policy, list_recurring_schedules]


def _transactions_tools():
    from users_repo import DEMO_USER_ID

    @tool
    def recent_transactions(limit: int = 5) -> str:
        """List the user's most recent payment transactions: amount, payee, status, and date."""
        rows = list_recent_transactions(DEMO_USER_ID, limit=limit)
        if not rows:
            return "No transactions yet."
        lines = []
        for r in rows:
            amount = r["amount_cents"] / 100
            payee = r["payee_nickname"] or "(no payee on record)"
            lines.append(
                f"- {r['currency']} {amount:.2f} to {payee}: {r['status']} on {r['created_at']} "
                f"(txn_id {r['txn_id']})"
            )
        return "\n".join(lines)

    @tool
    def transaction_status(txn_id: str) -> str:
        """Look up one specific transaction's status by its txn_id."""
        txn = get_transaction(txn_id)
        if not txn:
            return f"No transaction found with id {txn_id}."
        amount = txn["amount_cents"] / 100
        return (
            f"Transaction {txn_id}: {txn['currency']} {amount:.2f}, status={txn['status']}, "
            f"stripe_payment_intent_id={txn.get('stripe_payment_intent_id')}, created_at={txn['created_at']}"
        )

    @tool
    def check_receiver_balance(contact_name: str) -> str:
        """Check a saved contact's real Stripe receiver balance by name, to confirm they actually got paid."""
        matches = find_payees_by_name(DEMO_USER_ID, contact_name)
        if not matches:
            return f'No contact named "{contact_name}" found.'
        if len(matches) > 1:
            names = ", ".join(m["nickname"] for m in matches)
            return f'Multiple contacts match "{contact_name}": {names}. Ask the user which one they mean.'
        payee = matches[0]
        if not payee.get("stripe_connected_account_id"):
            return f"{payee['nickname']} has no receiver account on file, so their balance can't be checked."
        try:
            balance = get_account_balance(payee["stripe_connected_account_id"])
        except Exception as e:
            return f"Could not fetch {payee['nickname']}'s balance right now: {e}"
        total = (balance["available_cents"] + balance["pending_cents"]) / 100
        return f"{payee['nickname']} has {balance['currency'].upper()} {total:.2f} in their Stripe balance."

    return [recent_transactions, transaction_status, check_receiver_balance]


def _bills_tools():
    from users_repo import DEMO_USER_ID
    from services_repo import list_providers, list_invoices
    from bpay_repo import list_saved_billers, lookup_biller, find_biller_by_name

    @tool
    def list_service_bills() -> str:
        """List the user's service-provider invoices (utilities, telco, health, transport): amount, due date, and status."""
        rows = list_invoices(DEMO_USER_ID)
        if not rows:
            return "No service bills on file."
        lines = []
        for r in rows:
            amount = r["amount_cents"] / 100
            lines.append(
                f"- {r['provider_name']} ({r['category']}): {r['currency']} {amount:.2f}, "
                f"{r['status']}, due {r['due_date']} (invoice {r['invoice_id']}, ref {r['customer_ref']})"
            )
        return "\n".join(lines)

    @tool
    def list_service_providers() -> str:
        """List every known service provider (utility, telco, health, transport)."""
        rows = list_providers()
        if not rows:
            return "No service providers on file."
        return "\n".join(f"- {r['name']} ({r['category']})" for r in rows)

    @tool
    def list_my_bpay_billers() -> str:
        """List the user's own saved BPAY billers: nickname, biller code, and CRN."""
        rows = list_saved_billers(DEMO_USER_ID)
        if not rows:
            return "No saved BPAY billers yet."
        return "\n".join(f"- {r['nickname']}: biller code {r['biller_code']}, CRN {r['crn']}" for r in rows)

    @tool
    def lookup_bpay_biller(name_or_code: str) -> str:
        """Look up a BPAY biller in the registered directory by its code or name (e.g. 'Origin Energy' or '111999')."""
        biller = lookup_biller(name_or_code) if name_or_code.strip().isdigit() else find_biller_by_name(name_or_code)
        if not biller:
            return f'No registered BPAY biller matches "{name_or_code}".'
        return (
            f"{biller['biller_name']}: biller code {biller['biller_code']}, "
            f"amount range {biller['min_amount_cents'] / 100:.2f}-{biller['max_amount_cents'] / 100:.2f} AUD"
        )

    return [list_service_bills, list_service_providers, list_my_bpay_billers, lookup_bpay_biller]


def get_support_reply(user_id: str, message: str) -> str:
    """
    Runs one turn through the multi-agent support graph: a router picks the
    specialist whose domain the message falls into (account, transactions,
    bills, or general), and that specialist answers using only its own
    tools. The thread is keyed by user_id and stored in Postgres, so the
    conversation survives restarts, cold starts, and a mid-conversation
    switch between specialists.
    """
    result = _get_graph().invoke(
        {"messages": [HumanMessage(content=message)]},
        config={"configurable": {"thread_id": user_id}},
    )
    return _extract_text(result["messages"][-1].content)


def _extract_text(content) -> str:
    """
    Newer Gemini models return `content` as a list of typed blocks (text,
    plus internal reasoning/signature metadata) — sometimes as a real Python
    list, sometimes (observed with gemini-3.6-flash via langchain-google-genai
    0.4.4, seemingly depending on whether a tool was called first) already
    stringified into that list's repr. Either way, pull out just the text
    blocks so internal metadata/signatures never leak into what the user
    sees or what gets persisted as chat history.
    """
    if isinstance(content, str) and content.strip().startswith("[{"):
        try:
            content = ast.literal_eval(content)
        except (ValueError, SyntaxError):
            pass  # not actually a stringified block list -- fall through and use it as-is

    if isinstance(content, list):
        parts = [
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        joined = "".join(parts).strip()
        if joined:
            return joined
        return str(content)

    return content if isinstance(content, str) else str(content)
