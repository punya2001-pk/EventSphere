from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List
from sqlalchemy.orm import Session
from app.models import Venue, Event, VenueHireRequest
from app.config import VENUE_TURNAROUND_MINUTES
from app.agents.base import BaseAgent
from app.agents.protocol import ACLMessage, Performative

def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

class VenueAgent(BaseAgent):
    name = "VenueAgent"

    def default_beliefs(self) -> Dict[str, Any]:
        return {
            "turnaround_minutes": VENUE_TURNAROUND_MINUTES,
            "clashes_prevented": 0,
            "auto_confirmed_hires": 0,
            "escalated_hires": 0,
        }

    def _has_clash(self, db: Session, venue_id: int, start: datetime, end: datetime, ignore_event_id: int = None) -> str | None:
        buf = timedelta(minutes=VENUE_TURNAROUND_MINUTES)
        req_start = _ensure_utc(start) - buf
        req_end = _ensure_utc(end) + buf

        events = db.query(Event).filter(
            Event.venue_id == venue_id,
            Event.status != "cancelled"
        ).all()
        for ev in events:
            if ignore_event_id and ev.id == ignore_event_id:
                continue
            if req_start < _ensure_utc(ev.end_time) and req_end > _ensure_utc(ev.start_time):
                return f"Clashes with event '{ev.title}' (including {VENUE_TURNAROUND_MINUTES}-min turnaround buffer)."

        hires = db.query(VenueHireRequest).filter(
            VenueHireRequest.venue_id == venue_id,
            VenueHireRequest.status == "CONFIRMED"
        ).all()
        for h in hires:
            if req_start < _ensure_utc(h.end_time) and req_end > _ensure_utc(h.start_time):
                return f"Clashes with confirmed private hire #{h.id} (including {VENUE_TURNAROUND_MINUTES}-min turnaround buffer)."
        return None

    def _calculate_price(self, venue: Venue, start: datetime, end: datetime) -> tuple[float, str]:
        s = _ensure_utc(start)
        e = _ensure_utc(end)
        hours = max(1.0, (e - s).total_seconds() / 3600.0)
        is_weekend = s.weekday() >= 5 or (s.weekday() == 4 and s.hour >= 17)
        is_evening = s.hour >= 18
        multiplier = 1.25 if (is_weekend or is_evening) else 1.0
        note = "Peak/Weekend rate (1.25x)" if multiplier > 1.0 else "Standard off-peak rate (1.0x)"
        return round(hours * venue.hourly_rate * multiplier, 2), note

    def _find_alternatives(self, db: Session, venue: Venue, start: datetime, end: datetime, guests: int) -> List[Dict[str, Any]]:
        alternatives = []
        duration = _ensure_utc(end) - _ensure_utc(start)
        # 1. Alternative times on the same venue (+2h, +4h, +24h)
        for offset_hours in [2, 4, 24]:
            cand_start = _ensure_utc(start) + timedelta(hours=offset_hours)
            cand_end = cand_start + duration
            if not self._has_clash(db, venue.id, cand_start, cand_end):
                price, rate_note = self._calculate_price(venue, cand_start, cand_end)
                alternatives.append({
                    "type": "alternative_time",
                    "venue_id": venue.id,
                    "venue_name": venue.name,
                    "start_time": cand_start.isoformat(),
                    "end_time": cand_end.isoformat(),
                    "quoted_price": price,
                    "note": rate_note,
                })
                if len(alternatives) >= 2:
                    break

        # 2. Alternative open venues at the requested time
        other_venues = db.query(Venue).filter(
            Venue.id != venue.id,
            Venue.open_for_private_hire == True,
            Venue.capacity >= guests
        ).all()
        for ov in other_venues:
            if not self._has_clash(db, ov.id, start, end):
                price, rate_note = self._calculate_price(ov, start, end)
                alternatives.append({
                    "type": "alternative_venue",
                    "venue_id": ov.id,
                    "venue_name": ov.name,
                    "start_time": _ensure_utc(start).isoformat(),
                    "end_time": _ensure_utc(end).isoformat(),
                    "quoted_price": price,
                    "note": rate_note,
                })
        return alternatives

    async def receive(self, message: ACLMessage, db: Session, bus: Any) -> ACLMessage:
        beliefs = self.get_beliefs(db)
        intent = message.intent
        data = message.content

        # 1. Admin creating/publishing an event -> check calendar clash
        if intent == "check_event_slot":
            venue = db.query(Venue).filter(Venue.id == data["venue_id"]).first()
            if not venue:
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.REFUSE, intent=intent,
                    content={"reason": "Selected venue does not exist."}
                )
            start = datetime.fromisoformat(data["start_time"])
            end = datetime.fromisoformat(data["end_time"])
            if end <= start:
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.REFUSE, intent=intent,
                    content={"reason": "Event end time must be after start time."}
                )
            clash = self._has_clash(db, venue.id, start, end, ignore_event_id=data.get("ignore_event_id"))
            if clash:
                beliefs["clashes_prevented"] += 1
                self.update_beliefs(db, beliefs)
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.REFUSE, intent=intent,
                    content={"reason": clash}
                )
            return ACLMessage(
                conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                performative=Performative.AGREE, intent=intent,
                content={"venue_id": venue.id, "message": "Slot available with 60-min buffer verified."}
            )

        # 2. Attendee requesting private venue hire
        if intent == "request_private_hire":
            venue = db.query(Venue).filter(Venue.id == data["venue_id"]).first()
            if not venue or not venue.open_for_private_hire:
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.REFUSE, intent=intent,
                    content={"reason": "This venue is not open for private hire."}
                )
            guests = int(data["guest_count"])
            if guests > venue.capacity or guests <= 0:
                alts = self._find_alternatives(
                    db, venue, datetime.fromisoformat(data["start_time"]), datetime.fromisoformat(data["end_time"]), guests
                )
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.PROPOSE if alts else Performative.REFUSE,
                    intent=intent,
                    content={
                        "reason": f"Guest count ({guests}) exceeds {venue.name} capacity ({venue.capacity}).",
                        "alternatives": alts,
                    }
                )

            start = datetime.fromisoformat(data["start_time"])
            end = datetime.fromisoformat(data["end_time"])
            if end <= start:
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.REFUSE, intent=intent,
                    content={"reason": "End time must be after start time."}
                )

            clash = self._has_clash(db, venue.id, start, end)
            if clash:
                beliefs["clashes_prevented"] += 1
                self.update_beliefs(db, beliefs)
                alts = self._find_alternatives(db, venue, start, end, guests)
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.PROPOSE if alts else Performative.REFUSE,
                    intent=intent,
                    content={"reason": clash, "alternatives": alts}
                )

            price, rate_note = self._calculate_price(venue, start, end)
            hours = (_ensure_utc(end) - _ensure_utc(start)).total_seconds() / 3600.0

            # Escalation rule: > 4 hours OR > 100 guests requires Admin review
            needs_escalation = hours > 4.0 or guests > 100
            status = "ESCALATED" if needs_escalation else "CONFIRMED"

            hire = VenueHireRequest(
                user_id=data["user_id"],
                venue_id=venue.id,
                start_time=_ensure_utc(start),
                end_time=_ensure_utc(end),
                purpose=data["purpose"],
                guest_count=guests,
                quoted_price=price,
                status=status,
                agent_notes=f"{rate_note}. {'Escalated to Admin (>4h or >100 guests).' if needs_escalation else 'Auto-confirmed by VenueAgent.'}",
                conversation_id=message.conversation_id,
            )
            db.add(hire)
            db.commit()
            db.refresh(hire)

            if needs_escalation:
                beliefs["escalated_hires"] += 1
            else:
                beliefs["auto_confirmed_hires"] += 1
            self.update_beliefs(db, beliefs)

            # Broadcast INFORM so NotificationAgent alerts the attendee
            await bus.send(ACLMessage(
                conversation_id=message.conversation_id,
                sender=self.name,
                receiver="BROADCAST",
                performative=Performative.INFORM,
                intent="venue_hire_updated",
                content={
                    "user_id": data["user_id"],
                    "venue_name": venue.name,
                    "status": status,
                    "quoted_price": price,
                    "hire_id": hire.id,
                }
            ), db)

            return ACLMessage(
                conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                performative=Performative.AGREE, intent=intent,
                content={
                    "hire_id": hire.id,
                    "status": status,
                    "quoted_price": price,
                    "rate_note": rate_note,
                    "message": hire.agent_notes,
                }
            )

        return ACLMessage(
            conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
            performative=Performative.REFUSE, intent=intent, content={"reason": f"Unsupported intent: {intent}"}
        )