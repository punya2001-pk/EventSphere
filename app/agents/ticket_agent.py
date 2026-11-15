import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, Any
from sqlalchemy.orm import Session
from app.models import Ticket, Event
from app.config import SUSPICIOUS_SCAN_THRESHOLD
from app.agents.base import BaseAgent
from app.agents.protocol import ACLMessage, Performative

def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

class TicketAgent(BaseAgent):
    name = "TicketAgent"

    def default_beliefs(self) -> Dict[str, Any]:
        return {
            "tickets_issued": 0,
            "tickets_admitted": 0,
            "rejected_scans": 0,
            "scan_history": {},          # code -> attempt count
            "suspicious_codes": [],      # codes scanned >= SUSPICIOUS_SCAN_THRESHOLD
        }

    async def receive(self, message: ACLMessage, db: Session, bus: Any) -> ACLMessage:
        beliefs = self.get_beliefs(db)
        intent = message.intent
        data = message.content

        # 1. Mint unique unguessable tickets
        if intent == "issue_tickets":
            codes = []
            for _ in range(int(data["quantity"])):
                code = f"EVT-{secrets.token_urlsafe(12)}"
                t = Ticket(
                    booking_id=data["booking_id"],
                    event_id=data["event_id"],
                    category_id=data["category_id"],
                    owner_id=data["owner_id"],
                    code=code,
                    status="VALID",
                )
                db.add(t)
                codes.append(code)
            db.commit()

            beliefs["tickets_issued"] += len(codes)
            self.update_beliefs(db, beliefs)

            return ACLMessage(
                conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                performative=Performative.INFORM, intent=intent,
                content={"codes": codes}
            )

        # 2. Void tickets when booking is cancelled
        if intent == "void_booking_tickets":
            tickets = db.query(Ticket).filter(Ticket.booking_id == data["booking_id"]).all()
            for t in tickets:
                t.status = "CANCELLED"
            db.commit()
            return ACLMessage(
                conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                performative=Performative.INFORM, intent=intent,
                content={"voided_count": len(tickets)}
            )

        # 3. Gate Scan Verification
        if intent == "validate_scan":
            code = data["code"].strip()
            expected_event_id = int(data["event_id"])

            # Track repeated scans in persistent belief state
            history = dict(beliefs.get("scan_history", {}))
            history[code] = history.get(code, 0) + 1
            beliefs["scan_history"] = history
            suspicious = False
            if history[code] >= SUSPICIOUS_SCAN_THRESHOLD:
                suspicious = True
                sus_list = list(beliefs.get("suspicious_codes", []))
                if code not in sus_list:
                    sus_list.append(code)
                beliefs["suspicious_codes"] = sus_list

            ticket = db.query(Ticket).filter(Ticket.code == code).first()
            if not ticket:
                beliefs["rejected_scans"] += 1
                self.update_beliefs(db, beliefs)
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.REFUSE, intent=intent,
                    content={"verdict": "INVALID", "reason": "Ticket code not recognized.", "suspicious": suspicious}
                )

            ticket.scan_attempts += 1

            if ticket.event_id != expected_event_id:
                db.commit()
                beliefs["rejected_scans"] += 1
                self.update_beliefs(db, beliefs)
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.REFUSE, intent=intent,
                    content={"verdict": "WRONG_EVENT", "reason": f"Ticket belongs to '{ticket.event.title}', not this event.", "suspicious": suspicious}
                )

            if ticket.status == "CANCELLED":
                db.commit()
                beliefs["rejected_scans"] += 1
                self.update_beliefs(db, beliefs)
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.REFUSE, intent=intent,
                    content={"verdict": "CANCELLED", "reason": "This ticket was cancelled and refunded.", "suspicious": suspicious}
                )

            if ticket.status == "USED":
                db.commit()
                beliefs["rejected_scans"] += 1
                self.update_beliefs(db, beliefs)
                msg = f"Already used at {ticket.checked_in_at.strftime('%H:%M UTC')}."
                if suspicious:
                    msg += " WARNING: Repeated scan threshold exceeded (possible shared screenshot)!"
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.REFUSE, intent=intent,
                    content={"verdict": "ALREADY_USED", "reason": msg, "suspicious": suspicious}
                )

            # Check-in window rule: opens 2 hours before event start, closes at event end
            event = db.query(Event).filter(Event.id == ticket.event_id).first()
            now = datetime.now(timezone.utc)
            window_open = _ensure_utc(event.start_time) - timedelta(hours=2)
            window_close = _ensure_utc(event.end_time)
            if not data.get("bypass_window", False) and (now < window_open or now > window_close):
                db.commit()
                beliefs["rejected_scans"] += 1
                self.update_beliefs(db, beliefs)
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.REFUSE, intent=intent,
                    content={"verdict": "OUTSIDE_TIME", "reason": "Outside check-in window (opens 2h before event until end).", "suspicious": suspicious}
                )

            # Admit ticket!
            ticket.status = "USED"
            ticket.checked_in_at = now
            db.commit()

            beliefs["tickets_admitted"] += 1
            self.update_beliefs(db, beliefs)

            return ACLMessage(
                conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                performative=Performative.AGREE, intent=intent,
                content={
                    "verdict": "ADMIT",
                    "reason": f"Admitted: {ticket.owner.name} ({ticket.category.name})",
                    "attendee": ticket.owner.name,
                    "category": ticket.category.name,
                    "suspicious": suspicious,
                }
            )

        return ACLMessage(
            conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
            performative=Performative.REFUSE, intent=intent, content={"reason": f"Unsupported intent: {intent}"}
        )