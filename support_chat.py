import os
from datetime import datetime, timezone
from uuid import uuid4

from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.tools import tool
from langchain.agents import create_agent

from db import get_conn
from payees_repo import find_payees_by_name
from transactions_repo import list_recent_transactions, get_transaction
from stripe_service import get_account_balance

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

SYSTEM_PROMPT = (
    "You are VocalPay's customer support assistant. Answer questions about the user's contacts, "
    "payments, and receiver balances using your tools -- never guess or invent an amount, status, "
    "name, or PayID; if a tool says something wasn't found, say so plainly instead of making it up. "
    "You cannot move money yourself. If the user asks you to pay or send money to someone, tell them "
    "to type a payment command directly in the chat (e.g. 'Pay 12 to John') -- real transfers must go "
    "through the app's own confirmation/PIN flow, which you don't have access to. "
    "Keep answers short and conversational, like a real support agent, not a wall of text."
)

# One agent instance per process — cheap to reuse, and avoids re-authenticating
# with Gemini on every single chat message.
_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        from langchain_google_genai import ChatGoogleGenerativeAI

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("Missing GEMINI_API_KEY in environment/.env")
        model = ChatGoogleGenerativeAI(model=GEMINI_MODEL, google_api_key=api_key, temperature=0.3)
        _agent = create_agent(model, tools=_build_tools(), system_prompt=SYSTEM_PROMPT)
    return _agent


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


def _load_history(user_id: str, limit: int = 20) -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT role, content FROM chat_messages WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    finally:
        conn.close()
    rows = list(reversed(rows))
    return [
        HumanMessage(content=r["content"]) if r["role"] == "human" else AIMessage(content=r["content"])
        for r in rows
    ]


def _save_message(user_id: str, role: str, content: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO chat_messages (message_id, user_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid4()), user_id, role, content, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_support_reply(user_id: str, message: str) -> str:
    """
    Runs the user's message through the support agent, with their past
    conversation (persisted in the DB per-user, not per-session, so it
    survives closing the tab or the service restarting) as context, then
    persists both sides of this turn for next time.
    """
    agent = _get_agent()
    history = _load_history(user_id)
    result = agent.invoke({"messages": history + [HumanMessage(content=message)]})
    reply = result["messages"][-1].content
    if not isinstance(reply, str):
        reply = str(reply)

    _save_message(user_id, "human", message)
    _save_message(user_id, "ai", reply)
    return reply
