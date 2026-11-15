from enum import Enum
from typing import Dict, Any
from pydantic import BaseModel, Field
import uuid

class Performative(str, Enum):
    REQUEST = "REQUEST"
    AGREE = "AGREE"
    REFUSE = "REFUSE"
    PROPOSE = "PROPOSE"
    INFORM = "INFORM"
    FAILURE = "FAILURE"

class ACLMessage(BaseModel):
    conversation_id: str = Field(default_factory=lambda: f"conv-{uuid.uuid4().hex[:10]}")
    sender: str
    receiver: str  # Specific agent name OR "BROADCAST"
    performative: Performative
    intent: str
    content: Dict[str, Any] = Field(default_factory=dict)