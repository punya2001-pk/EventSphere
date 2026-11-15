from datetime import datetime, timedelta, timezone
from typing import Dict, Any
from sqlalchemy.orm import Session
from app.models import WaitlistEntry, TicketCategory
from app.config import WAITLIST_HOLD_MINUTES
from app.agents.base import BaseAgent
from app.agents.protocol import ACLMessage, Performative

def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

class WaitlistAgent(BaseAgent):
    name = "WaitlistAgent"

    def default_beliefs(self) -> Dict[str, Any]:
        return {
            "hold_minutes": WAITLIST_HOLD_MINUTES,
            "offers_made": 0,
            "expired_holds_reclaimed": 0,
            "last_sweep_at": None,
        }

    def _allocate_to_next_in_line(self, db: Session, category_id: int) -> list[WaitlistEntry]:
        cat = db.query(TicketCategory).filter(TicketCategory.id == category_id).first()
        if not cat or cat.available <= 0:
            return []

        waiting = (
            db.query(WaitlistEntry)
            .filter(WaitlistEntry.category_id == category_id, WaitlistEntry.status == "WAITING")
            .order_by(WaitlistEntry.created_at.asc())
            .all()
        )
        offered_entries = []
        for entry in waiting:
            if cat.available >= entry.quantity:
                cat.held_quantity += entry.quantity
                entry.status = "OFFERED"
                entry.hold_expires_at = datetime.now(timezone.utc) + timedelta(minutes=WAITLIST_HOLD_MINUTES)
                offered_entries.append(entry)
        db.commit()
        return offered_entries

    async def proactive_expiry_sweep(self, db: Session, bus: Any):
        """Proactive scheduled task: runs automatically with zero user input."""
        now = datetime.now(timezone.utc)
        beliefs = self.get_beliefs(db)
        beliefs["last_sweep_at"] = now.isoformat()

        expired = (
            db.query(WaitlistEntry)
            .filter(WaitlistEntry.status == "OFFERED", WaitlistEntry.hold_expires_at != None)
            .all()
        )
        affected_categories = set()
        reclaimed_count = 0

        for entry in expired:
            if _ensure_utc(entry.hold_expires_at) <= now:
                entry.status = "EXPIRED"
                cat = db.query(TicketCategory).filter(TicketCategory.id == entry.category_id).first()
                if cat:
                    cat.held_quantity = max(0, cat.held_quantity - entry.quantity)
                    affected_categories.add(cat.id)
                reclaimed_count += 1

        if reclaimed_count > 0:
            db.commit()
            beliefs["expired_holds_reclaimed"] += reclaimed_count
            self.update_beliefs(db, beliefs)

            # Log proactive sweep and re-offer freed seats to next waiting users
            for cat_id in affected_categories:
                new_offers = self._allocate_to_next_in_line(db, cat_id)
                for offer in new_offers:
                    beliefs["offers_made"] += 1
                    self.update_beliefs(db, beliefs)
                    await bus.send(ACLMessage(
                        sender=self.name,
                        receiver="BROADCAST",
                        performative=Performative.INFORM,
                        intent="waitlist_seat_offered",
                        content={
                            "user_id": offer.user_id,
                            "event_title": offer.event.title,
                            "category_name": offer.category.name,
                            "quantity": offer.quantity,
                            "expires_at": offer.hold_expires_at.isoformat(),
                        }
                    ), db)
        else:
            self.update_beliefs(db, beliefs)

    async def receive(self, message: ACLMessage, db: Session, bus: Any) -> ACLMessage:
        beliefs = self.get_beliefs(db)
        intent = message.intent
        data = message.content

        # 1. Attendee joins waitlist
        if intent == "join_waitlist":
            cat = db.query(TicketCategory).filter(TicketCategory.id == data["category_id"]).first()
            if not cat:
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.REFUSE, intent=intent, content={"reason": "Category not found."}
                )
            entry = WaitlistEntry(
                user_id=data["user_id"],
                event_id=cat.event_id,
                category_id=cat.id,
                quantity=int(data.get("quantity", 1)),
                status="WAITING",
            )
            db.add(entry)
            db.commit()
            return ACLMessage(
                conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                performative=Performative.AGREE, intent=intent,
                content={"waitlist_id": entry.id, "message": "Added to FIFO waitlist."}
            )

        # 2. Reactive response to BookingAgent's seats_released message
        if intent == "seats_released":
            offered = self._allocate_to_next_in_line(db, int(data["category_id"]))
            for offer in offered:
                beliefs["offers_made"] += 1
                self.update_beliefs(db, beliefs)
                await bus.send(ACLMessage(
                    conversation_id=message.conversation_id,
                    sender=self.name,
                    receiver="BROADCAST",
                    performative=Performative.INFORM,
                    intent="waitlist_seat_offered",
                    content={
                        "user_id": offer.user_id,
                        "event_title": offer.event.title,
                        "category_name": offer.category.name,
                        "quantity": offer.quantity,
                        "expires_at": offer.hold_expires_at.isoformat(),
                    }
                ), db)
            return ACLMessage(
                conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                performative=Performative.INFORM, intent=intent,
                content={"offers_dispatched": len(offered)}
            )

        return ACLMessage(
            conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
            performative=Performative.REFUSE, intent=intent, content={"reason": f"Unsupported intent: {intent}"}
        )