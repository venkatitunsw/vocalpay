import ast
import os

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
from payees_repo import find_payees_by_name
from transactions_repo import list_recent_transactions, get_transaction
from stripe_service import get_account_balance

# Self-hosted only, deliberately -- no cloud LLM API (Gemini, OpenAI, etc.)
# anywhere in this app. Needs a real Ollama server reachable at
# OLLAMA_BASE_URL; no API key involved.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
# Bounds how long a single Ollama call can run server-side. /support/chat
# runs as a plain sync endpoint (FastAPI's sync thread pool), so a hung call
# without this would tie up a worker thread indefinitely -- enough of those
# stacking up could stall unrelated endpoints sharing the same pool. The
# frontend's own ~120s AbortController timeout is a backstop for this, not
# the primary defense.
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "90"))

SYSTEM_PROMPT = (
    "You are VocalPay's customer support assistant. Answer questions about the user's contacts, "
    "payments, and receiver balances using your tools -- never guess or invent an amount, status, "
    "name, or PayID; if a tool says something wasn't found, say so plainly instead of making it up. "
    "You cannot move money yourself. If the user asks you to pay or send money to someone, tell them "
    "to type a payment command directly in the chat (e.g. 'Pay 12 to John') -- real transfers must go "
    "through the app's own confirmation/PIN flow, which you don't have access to. "
    "Keep answers short and conversational, like a real support agent, not a wall of text."
)

# Prompt window: the checkpointer keeps the full thread, but only the most recent
# messages are sent to the model on each turn.
MEMORY_WINDOW_MESSAGES = int(os.getenv("MEMORY_WINDOW_MESSAGES", "20"))

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
    kwargs = {"autocommit": True, "row_factory": dict_row}
    if db.DB_SCHEMA:
        kwargs["options"] = f"-c search_path={db.DB_SCHEMA}"
    _saver_conn = psycopg.connect(db.DATABASE_URL, **kwargs)
    saver = PostgresSaver(_saver_conn)
    saver.setup()
    return saver


def _build_graph():
    tools = _build_tools()
    bound_model = _build_model().bind_tools(tools)

    def call_model(state: MessagesState):
        window = trim_messages(
            state["messages"],
            max_tokens=MEMORY_WINDOW_MESSAGES,
            token_counter=len,
            strategy="last",
            start_on="human",
            include_system=False,
        )
        response = bound_model.invoke([SystemMessage(content=SYSTEM_PROMPT)] + window)
        return {"messages": [response]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", call_model)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
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


def _build_tools():
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

    return [list_contacts, recent_transactions, transaction_status, check_receiver_balance, explain_payment_policy]


def get_support_reply(user_id: str, message: str) -> str:
    """
    Runs one turn through the LangGraph support agent. The thread is keyed by
    user_id and stored in Postgres, so the conversation survives restarts and
    cold starts.
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
