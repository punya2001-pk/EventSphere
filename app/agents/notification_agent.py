from typing import Dict, Any
from sqlalchemy.orm import Session
from app.models import Notification
from app.agents.base import BaseAgent
from app.agents.protocol import ACLMessage, Performative

class NotificationAgent(BaseAgent):
    name = "NotificationAgent"

    def default_beliefs(self) -> Dict[str, Any]:
        return {"notifications_sent": 0}

    async def receive(self, message: ACLMessage, db: Session, bus: Any) -> ACLMessage:
        if message.performative != Performative.INFORM:
            return ACLMessage(
                conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                performative=Performative.REFUSE, intent=message.intent,
                content={"reason": "NotificationAgent only reacts to INFORM messages."}
            )

        data = message.content
        user_id = data.get("user_id")
        if not user_id:
            return ACLMessage(
                conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
                performative=Performative.INFORM, intent="ignored", content={"reason": "No target user_id"}
            )

        intent = message.intent
        title = "EventSphere Update"
        body = ""

        if intent == "booking_confirmed":
            title = "Booking Confirmed"
            body = f"You booked {data['quantity']} ticket(s) for {data['event_title']}. Ref: {data['payment_ref']}."
        elif intent == "booking_cancelled":
            title = "Booking Cancelled & Refund Processed"
            body = f"Your booking for {data['event_title']} was cancelled. Refund: ${data['refund_amount']:.2f} ({data['policy_note']})."
        elif intent == "venue_hire_updated":
            title = f"Venue Hire {data['status']}"
            body = f"Your hire request for {data['venue_name']} is {data['status']}. Quoted price: ${data['quoted_price']:.2f}."
        elif intent == "waitlist_seat_offered":
            title = "Waitlist Seat Reserved for You!"
            body = f"{data['quantity']} seat(s) in {data['category_name']} for {data['event_title']} are held for you for 10 minutes!"

        if body:
            notif = Notification(user_id=user_id, title=title, message=body)
            db.add(notif)
            db.commit()

            beliefs = self.get_beliefs(db)
            beliefs["notifications_sent"] += 1
            self.update_beliefs(db, beliefs)

        return ACLMessage(
            conversation_id=message.conversation_id, sender=self.name, receiver=message.sender,
            performative=Performative.INFORM, intent="notification_created",
            content={"user_id": user_id, "title": title}
        )