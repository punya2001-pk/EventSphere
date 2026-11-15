from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, JSON
)
from sqlalchemy.orm import relationship
from app.database import Base

def utcnow():
    return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    email = Column(String(150), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="attendee", nullable=False)  # admin | staff | attendee
    created_at = Column(DateTime(timezone=True), default=utcnow)

class Venue(Base):
    __tablename__ = "venues"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    location = Column(String(200), nullable=False)
    capacity = Column(Integer, nullable=False)
    description = Column(Text, default="")
    image_url = Column(String(500), default="https://images.unsplash.com/photo-1517457373958-b7bdd4587205?w=800")
    hourly_rate = Column(Float, nullable=False)
    open_for_private_hire = Column(Boolean, default=True)

class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    category = Column(String(50), nullable=False)  # Concert | Workshop | Seminar | Conference
    venue_id = Column(Integer, ForeignKey("venues.id"), nullable=False)
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=False)
    description = Column(Text, default="")
    status = Column(String(20), default="draft")  # draft | published | cancelled
    venue = relationship("Venue")
    categories = relationship("TicketCategory", back_populates="event", cascade="all, delete-orphan")

class TicketCategory(Base):
    __tablename__ = "ticket_categories"
    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    name = Column(String(80), nullable=False)  # e.g. General, VIP, Student
    price = Column(Float, nullable=False)
    total_quantity = Column(Integer, nullable=False)
    sold_quantity = Column(Integer, default=0, nullable=False)
    held_quantity = Column(Integer, default=0, nullable=False)  # Active waitlist holds
    event = relationship("Event", back_populates="categories")

    @property
    def available(self) -> int:
        return max(0, self.total_quantity - self.sold_quantity - self.held_quantity)

class Booking(Base):
    __tablename__ = "bookings"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    category_id = Column(Integer, ForeignKey("ticket_categories.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    total_paid = Column(Float, nullable=False)
    refund_amount = Column(Float, default=0.0)
    payment_ref = Column(String(64), nullable=False)
    status = Column(String(20), default="CONFIRMED")  # CONFIRMED | CANCELLED
    conversation_id = Column(String(64), index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    user = relationship("User")
    event = relationship("Event")
    category = relationship("TicketCategory")
    tickets = relationship("Ticket", back_populates="booking")

class Ticket(Base):
    __tablename__ = "tickets"
    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    category_id = Column(Integer, ForeignKey("ticket_categories.id"), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    code = Column(String(64), unique=True, index=True, nullable=False)  # Unguessable token
    status = Column(String(20), default="VALID")  # VALID | USED | CANCELLED
    scan_attempts = Column(Integer, default=0)
    checked_in_at = Column(DateTime(timezone=True), nullable=True)

    booking = relationship("Booking", back_populates="tickets")
    event = relationship("Event")
    category = relationship("TicketCategory")
    owner = relationship("User")

class VenueHireRequest(Base):
    __tablename__ = "venue_hire_requests"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    venue_id = Column(Integer, ForeignKey("venues.id"), nullable=False)
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=False)
    purpose = Column(String(250), nullable=False)
    guest_count = Column(Integer, nullable=False)
    quoted_price = Column(Float, nullable=False)
    status = Column(String(20), default="PENDING")  # CONFIRMED | ESCALATED | REJECTED | COUNTER_PROPOSED
    agent_notes = Column(Text, default="")
    alternatives_json = Column(JSON, default=list)
    conversation_id = Column(String(64), index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    user = relationship("User")
    venue = relationship("Venue")

class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    category_id = Column(Integer, ForeignKey("ticket_categories.id"), nullable=False)
    quantity = Column(Integer, default=1, nullable=False)
    status = Column(String(20), default="WAITING")  # WAITING | OFFERED | CLAIMED | EXPIRED
    hold_expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    user = relationship("User")
    event = relationship("Event")
    category = relationship("TicketCategory")

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(150), nullable=False)
    message = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)

# --- MULTI-AGENT PERSISTENT STATE & MESSAGE TRACE TABLES ---

class AgentBelief(Base):
    """Stores persistent agent beliefs/state so agent memory survives server restarts."""
    __tablename__ = "agent_beliefs"
    id = Column(Integer, primary_key=True, index=True)
    agent_name = Column(String(50), unique=True, index=True, nullable=False)
    beliefs = Column(JSON, default=dict, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class AgentMessageLog(Base):
    """Complete FIPA-ACL message log for traceability and live viva inspection."""
    __tablename__ = "agent_message_logs"
    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(String(64), index=True, nullable=False)
    sender = Column(String(50), nullable=False)
    receiver = Column(String(50), nullable=False)
    performative = Column(String(20), nullable=False)  # REQUEST|AGREE|REFUSE|PROPOSE|INFORM|FAILURE
    intent = Column(String(80), nullable=False)
    content = Column(JSON, default=dict, nullable=False)
    timestamp = Column(DateTime(timezone=True), default=utcnow)