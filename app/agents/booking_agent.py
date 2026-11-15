import uuid
from datetime import datetime, timezone
from typing import Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.models import Event, TicketCategory, Booking, WaitlistEntry
from app.config import MAX_TICKETS_PER_BOOKING
from app.agents.base import BaseAgent
from app.agents.protocol import ACLMessage, Performative

def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

class BookingAgent(BaseAgent):
    name = "BookingAgent"

    def default_beliefs(self) -> Dict[str, Any]:
        return {
            "max_tickets_per_booking": MAX_TICKETS_PER_BOOKING,
            "total_tickets_sold": 0,
            "oversell_attempts_blocked": 0,
            "total_refunded": 0.0,
        }

    def _calculate_refund(self, booking: Booking, event: Event, forced_by_organiser: bool = False) -> tuple[float, str]:
        if forced_by_organiser:
            return round(booking.total_paid, 2), "100% refund (Event cancelled by organiser)"
        hours_until_event = (_ensure_utc(event.start_time) - datetime.now(timezone.utc)).total_seconds() / 3600.0
        if hours_until_event >= 48:
            return round(booking.total_paid, 2), "100% refund (>= 48h before event)"
        elif hours_until_event >= 24:
            return round(booking.total_paid * 0.5, 2), "50% refund (24h-48h before event)"
        return 0.0, "0% refund (< 24h before event)"

    async def receive(self, message: ACLMessage, db: Session, bus: Any) -> ACLMessage:
        beliefs = self.get_beliefs(db)
        intent = message.intent
        data = message.content

        # 1. Book Tickets (with concurrency lock)
        if intent == "book_tickets":
            qty = int(data["quantity"])
            if qty < 1 or qty > MAX_TICKETS_PER_BOOKING:
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.REFUSE, intent=intent,
                    content={"reason": f"Quantity must be between 1 and {MAX_TICKETS_PER_BOOKING} tickets per booking."}
                )

            try:
                # Concurrency control: serialize write lock on SQLite or SELECT FOR UPDATE on Postgres
                if db.bind.dialect.name == "sqlite":
                    db.execute(text("BEGIN IMMEDIATE"))

                cat = (
                    db.query(TicketCategory)
                    .filter(TicketCategory.id == data["category_id"])
                    .with_for_update()
                    .first()
                )
                if not cat:
                    db.rollback()
                    return ACLMessage(
                        conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                        performative=Performative.REFUSE, intent=intent,
                        content={"reason": "Ticket category not found."}
                    )

                event = db.query(Event).filter(Event.id == cat.event_id).first()
                if not event or event.status != "published":
                    db.rollback()
                    return ACLMessage(
                        conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                        performative=Performative.REFUSE, intent=intent,
                        content={"reason": "Event is not open for booking."}
                    )

                # Check if user is claiming an active waitlist hold
                hold_entry = None
                if data.get("waitlist_id"):
                    hold_entry = db.query(WaitlistEntry).filter(
                        WaitlistEntry.id == data["waitlist_id"],
                        WaitlistEntry.user_id == data["user_id"],
                        WaitlistEntry.status == "OFFERED"
                    ).first()

                effective_available = cat.available + (hold_entry.quantity if hold_entry else 0)
                if effective_available < qty:
                    db.rollback()
                    beliefs["oversell_attempts_blocked"] += 1
                    self.update_beliefs(db, beliefs)
                    return ACLMessage(
                        conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                        performative=Performative.REFUSE, intent=intent,
                        content={"reason": f"Only {effective_available} seat(s) left in '{cat.name}'."}
                    )

                if hold_entry:
                    cat.held_quantity = max(0, cat.held_quantity - hold_entry.quantity)
                    hold_entry.status = "CLAIMED"

                cat.sold_quantity += qty
                total_paid = round(cat.price * qty, 2)
                payment_ref = f"PAY-{uuid.uuid4().hex[:8].upper()}"

                booking = Booking(
                    user_id=data["user_id"],
                    event_id=event.id,
                    category_id=cat.id,
                    quantity=qty,
                    total_paid=total_paid,
                    payment_ref=payment_ref,
                    status="CONFIRMED",
                    conversation_id=message.conversation_id,
                )
                db.add(booking)
                db.commit()
                db.refresh(booking)
            except Exception as exc:
                db.rollback()
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.FAILURE, intent=intent,
                    content={"reason": f"Transaction aborted safely: {str(exc)}"}
                )

            beliefs["total_tickets_sold"] += qty
            self.update_beliefs(db, beliefs)

            # Ask TicketAgent via ACL message to mint unique QR codes
            ticket_reply = await bus.send(ACLMessage(
                conversation_id=message.conversation_id,
                sender=self.name,
                receiver="TicketAgent",
                performative=Performative.REQUEST,
                intent="issue_tickets",
                content={
                    "booking_id": booking.id,
                    "event_id": event.id,
                    "category_id": cat.id,
                    "owner_id": data["user_id"],
                    "quantity": qty,
                }
            ), db)

            # Inform NotificationAgent
            await bus.send(ACLMessage(
                conversation_id=message.conversation_id,
                sender=self.name,
                receiver="BROADCAST",
                performative=Performative.INFORM,
                intent="booking_confirmed",
                content={
                    "user_id": data["user_id"],
                    "event_title": event.title,
                    "quantity": qty,
                    "payment_ref": payment_ref,
                }
            ), db)

            return ACLMessage(
                conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                performative=Performative.AGREE, intent=intent,
                content={
                    "booking_id": booking.id,
                    "payment_ref": payment_ref,
                    "total_paid": total_paid,
                    "ticket_codes": ticket_reply.content.get("codes", []),
                }
            )

        # 2. Attendee Cancels Booking -> Release Seats -> Inform WaitlistAgent
        if intent == "cancel_booking":
            booking = db.query(Booking).filter(Booking.id == data["booking_id"]).first()
            if not booking or booking.status != "CONFIRMED":
                return ACLMessage(
                    conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                    performative=Performative.REFUSE, intent=intent,
                    content={"reason": "Booking not found or already cancelled."}
                )

            event = db.query(Event).filter(Event.id == booking.event_id).first()
            cat = db.query(TicketCategory).filter(TicketCategory.id == booking.category_id).first()
            refund, policy_note = self._calculate_refund(booking, event, forced_by_organiser=data.get("forced", False))

            booking.status = "CANCELLED"
            booking.refund_amount = refund
            cat.sold_quantity = max(0, cat.sold_quantity - booking.quantity)
            db.commit()

            beliefs["total_refunded"] = round(beliefs["total_refunded"] + refund, 2)
            self.update_beliefs(db, beliefs)

            # Tell TicketAgent to void the tickets
            await bus.send(ACLMessage(
                conversation_id=message.conversation_id,
                sender=self.name,
                receiver="TicketAgent",
                performative=Performative.REQUEST,
                intent="void_booking_tickets",
                content={"booking_id": booking.id}
            ), db)

            # Tell WaitlistAgent that seats were freed (if event still active)
            if not data.get("forced", False) and event.status == "published":
                await bus.send(ACLMessage(
                    conversation_id=message.conversation_id,
                    sender=self.name,
                    receiver="WaitlistAgent",
                    performative=Performative.INFORM,
                    intent="seats_released",
                    content={"event_id": event.id, "category_id": cat.id, "freed_seats": booking.quantity}
                ), db)

            # Broadcast notification
            await bus.send(ACLMessage(
                conversation_id=message.conversation_id,
                sender=self.name,
                receiver="BROADCAST",
                performative=Performative.INFORM,
                intent="booking_cancelled",
                content={
                    "user_id": booking.user_id,
                    "event_title": event.title,
                    "refund_amount": refund,
                    "policy_note": policy_note,
                }
            ), db)

            return ACLMessage(
                conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                performative=Performative.AGREE, intent=intent,
                content={"booking_id": booking.id, "refund_amount": refund, "policy_note": policy_note}
            )

        # 3. Organiser Cancels Whole Event -> Full Refund All Bookings
        if intent == "event_cancelled":
            event_id = data["event_id"]
            bookings = db.query(Booking).filter(Booking.event_id == event_id, Booking.status == "CONFIRMED").all()
            for b in bookings:
                await self.receive(ACLMessage(
                    conversation_id=message.conversation_id,
                    sender=self.name,
                    receiver=self.name,
                    performative=Performative.REQUEST,
                    intent="cancel_booking",
                    content={"booking_id": b.id, "forced": True}
                ), db, bus)
            return ACLMessage(
                conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                performative=Performative.INFORM, intent=intent,
                content={"refunded_bookings": len(bookings)}
            )

        return ACLMessage(
            conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
            performative=Performative.REFUSE, intent=intent, content={"reason": f"Unsupported intent: {intent}"}
        )