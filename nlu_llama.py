import json
import os
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError

# Llama only proposes payment parameters. Its output becomes a canonical sentence
# that the deterministic parser, policy engine, and PIN/CONFIRM steps all still
# handle. It never confirms, executes, or changes a transaction.
NLU_LLAMA_ENABLED = os.getenv("NLU_LLAMA", "0") == "1"
NLU_TIMEOUT_SECONDS = float(os.getenv("NLU_TIMEOUT_SECONDS", "60"))

NLU_SYSTEM_PROMPT = (
    "You extract one payment instruction from a user's message for a banking app. "
    "Return only JSON matching the schema. Use action 'pay' only for a clear request to send money; "
    "otherwise use 'none'. amount is a positive number in the stated currency, or null if not given. "
    "target is the payee name or PayID digits exactly as written, or null. "
    "If the message is unclear, contradictory, or has two different amounts without a correction, "
    "list the problem in ambiguities."
)


class NLUIntent(BaseModel):
    action: Literal["pay", "none"]
    amount: Optional[float] = None
    currency: str = "AUD"
    target: Optional[str] = None
    note: Optional[str] = None
    ambiguities: list[str] = Field(default_factory=list)


def _invoke_model(text: str) -> str:
    from langchain_ollama import ChatOllama
    from langchain_core.messages import HumanMessage, SystemMessage

    import support_chat

    model = ChatOllama(
        base_url=support_chat.OLLAMA_BASE_URL,
        model=support_chat.OLLAMA_MODEL,
        temperature=0,
        format=NLUIntent.model_json_schema(),
        client_kwargs={"timeout": NLU_TIMEOUT_SECONDS},
    )
    reply = model.invoke([SystemMessage(content=NLU_SYSTEM_PROMPT), HumanMessage(content=text)])
    return reply.content if isinstance(reply.content, str) else str(reply.content)


def parse_with_llama(text: str, invoke=_invoke_model) -> Optional[NLUIntent]:
    """Returns a validated intent, retrying once on bad JSON. None means fall back to the regex parser."""
    for _ in range(2):
        try:
            raw = invoke(text)
            return NLUIntent.model_validate(json.loads(raw))
        except (ValidationError, ValueError):
            continue
        except Exception:
            return None
    return None


def to_canonical_command(intent: NLUIntent) -> Optional[str]:
    """Builds the sentence the regex parser understands, or None if the intent isn't safe to act on."""
    if intent.action != "pay" or intent.ambiguities:
        return None
    if intent.amount is None or intent.amount <= 0:
        return None
    if intent.currency.upper() != "AUD":
        return None
    if not intent.target or not intent.target.strip():
        return None

    sentence = f"Pay {intent.amount:.2f} to {intent.target.strip()}"
    if intent.note and intent.note.strip():
        sentence += f" for {intent.note.strip()}"
    return sentence


def llm_canonical_command(text: str, invoke=_invoke_model) -> Optional[str]:
    intent = parse_with_llama(text, invoke=invoke)
    if intent is None:
        return None
    return to_canonical_command(intent)
