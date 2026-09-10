from pydantic import BaseModel, Field
from typing import Literal, Optional

class PaymentIntentParsed(BaseModel):
    action: Literal["pay"] = "pay"
    amount: float = Field(..., gt=0, description="Payment amount in currency units (e.g., 12.50)")
    currency: Literal["AUD"] = "AUD"
    payee_name: str = Field(..., min_length=1)
    note: Optional[str] = ""
