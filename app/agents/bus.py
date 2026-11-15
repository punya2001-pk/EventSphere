from typing import Dict, List
from sqlalchemy.orm import Session
from app.models import AgentMessageLog
from app.agents.protocol import ACLMessage, Performative
from app.agents.base import BaseAgent

class MessageBus:
    def __init__(self):
        self.agents: Dict[str, BaseAgent] = {}
        self.broadcast_subscribers: List[str] = []

    def register(self, agent: BaseAgent, subscribe_broadcast: bool = False):
        self.agents[agent.name] = agent
        if subscribe_broadcast and agent.name not in self.broadcast_subscribers:
            self.broadcast_subscribers.append(agent.name)

    def _log_message(self, db: Session, msg: ACLMessage):
        entry = AgentMessageLog(
            conversation_id=msg.conversation_id,
            sender=msg.sender,
            receiver=msg.receiver,
            performative=msg.performative.value,
            intent=msg.intent,
            content=msg.content,
        )
        db.add(entry)
        db.commit()

    async def send(self, message: ACLMessage, db: Session) -> ACLMessage:
        """Routes a structured ACLMessage to target agent(s) and logs every step."""
        self._log_message(db, message)

        # Reactive broadcast handling (e.g., INFORM events)
        if message.receiver == "BROADCAST":
            for sub_name in self.broadcast_subscribers:
                if sub_name != message.sender and sub_name in self.agents:
                    await self.agents[sub_name].receive(message, db, self)
            ack = ACLMessage(
                conversation_id=message.conversation_id,
                sender="MessageBus",
                receiver=message.sender,
                performative=Performative.INFORM,
                intent="broadcast_delivered",
                content={"delivered_to": self.broadcast_subscribers},
            )
            self._log_message(db, ack)
            return ack

        target = self.agents.get(message.receiver)
        if not target:
            err = ACLMessage(
                conversation_id=message.conversation_id,
                sender="MessageBus",
                receiver=message.sender,
                performative=Performative.FAILURE,
                intent=message.intent,
                content={"reason": f"Unknown receiver agent: {message.receiver}"},
            )
            self._log_message(db, err)
            return err

        reply = await target.receive(message, db, self)
        self._log_message(db, reply)

        # If an agent emits an INFORM reply, notify broadcast listeners reactively
        if reply.performative == Performative.INFORM and reply.receiver != "BROADCAST":
            for sub_name in self.broadcast_subscribers:
                if sub_name not in (reply.sender, reply.receiver) and sub_name in self.agents:
                    fwd = ACLMessage(
                        conversation_id=reply.conversation_id,
                        sender=reply.sender,
                        receiver=sub_name,
                        performative=Performative.INFORM,
                        intent=reply.intent,
                        content=reply.content,
                    )
                    self._log_message(db, fwd)
                    await self.agents[sub_name].receive(fwd, db, self)

        return reply

bus = MessageBus()