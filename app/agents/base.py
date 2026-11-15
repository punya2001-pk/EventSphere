from typing import Dict, Any
from sqlalchemy.orm import Session
from app.models import AgentBelief
from app.agents.protocol import ACLMessage

class BaseAgent:
    name: str = "BaseAgent"

    def get_beliefs(self, db: Session) -> Dict[str, Any]:
        record = db.query(AgentBelief).filter(AgentBelief.agent_name == self.name).first()
        if not record:
            record = AgentBelief(agent_name=self.name, beliefs=self.default_beliefs())
            db.add(record)
            db.commit()
            db.refresh(record)
        return dict(record.beliefs)

    def update_beliefs(self, db: Session, updates: Dict[str, Any]) -> Dict[str, Any]:
        record = db.query(AgentBelief).filter(AgentBelief.agent_name == self.name).first()
        if not record:
            current = self.default_beliefs()
            current.update(updates)
            record = AgentBelief(agent_name=self.name, beliefs=current)
            db.add(record)
        else:
            merged = dict(record.beliefs)
            merged.update(updates)
            record.beliefs = merged
        db.commit()
        return dict(record.beliefs)

    def default_beliefs(self) -> Dict[str, Any]:
        return {}

    async def receive(self, message: ACLMessage, db: Session, bus: Any) -> ACLMessage:
        raise NotImplementedError