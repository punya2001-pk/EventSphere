import asyncio
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from app.database import Base, get_db
from app.main import app, bus, venue_agent, booking_agent, ticket_agent, waitlist_agent, notification_agent
from app.models import User, Venue, Event, TicketCategory, Ticket, WaitlistEntry, Notification
from app.security import hash_password
from app.agents.protocol import ACLMessage, Performative

SQLALCHEMY_TEST_URL = "sqlite:///./test_eventsphere.db"
engine = create_engine(SQLALCHEMY_TEST_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    admin = User(name="Admin", email="admin@test.edu", password_hash=hash_password("pass"), role="admin")
    staff = User(name="Staff", email="staff@test.edu", password_hash=hash_password("pass"), role="staff")
    u1 = User(name="Alice", email="alice@test.edu", password_hash=hash_password("pass"), role="attendee")
    u2 = User(name="Bob", email="bob@test.edu", password_hash=hash_password("pass"), role="attendee")
    db.add_all([admin, staff, u1, u2])
    db.commit()

    v1 = Venue(name="Hall A", location="Campus", capacity=100, hourly_rate=100.0, open_for_private_hire=True)
    v2 = Venue(name="Hall B", location="North", capacity=150, hourly_rate=120.0, open_for_private_hire=True)
    db.add_all([v1, v2])
    db.commit()

    now = datetime.now(timezone.utc)
    ev = Event(
        title="Tech Fest", category="Conference", venue_id=v1.id,
        start_time=now + timedelta(days=3), end_time=now + timedelta(days=3, hours=3), status="published"
    )
    ev2 = Event(
        title="Other Concert", category="Concert", venue_id=v2.id,
        start_time=now + timedelta(days=4), end_time=now + timedelta(days=4, hours=2), status="published"
    )
    db.add_all([ev, ev2])
    db.commit()

    cat = TicketCategory(event_id=ev.id, name="General", price=20.0, total_quantity=1)
    db.add(cat)
    db.commit()
    db.close()
    yield
    Base.metadata.drop_all(bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


# 1. Concurrency / Overbooking Test (BookingAgent)
def test_booking_agent_prevents_overbooking_last_seat():
    async def run():
        db1, db2 = TestingSessionLocal(), TestingSessionLocal()
        cat = db1.query(TicketCategory).first()
        u1 = db1.query(User).filter(User.email == "alice@test.edu").first()
        u2 = db2.query(User).filter(User.email == "bob@test.edu").first()

        m1 = ACLMessage(sender="Test", receiver="BookingAgent", performative=Performative.REQUEST,
                        intent="book_tickets", content={"user_id": u1.id, "category_id": cat.id, "quantity": 1})
        m2 = ACLMessage(sender="Test", receiver="BookingAgent", performative=Performative.REQUEST,
                        intent="book_tickets", content={"user_id": u2.id, "category_id": cat.id, "quantity": 1})

        r1, r2 = await asyncio.gather(bus.send(m1, db1), bus.send(m2, db2))
        perfs = {r1.performative, r2.performative}
        assert Performative.AGREE in perfs
        assert Performative.REFUSE in perfs
        db1.close()
        db2.close()

    asyncio.run(run())


# 2. Max 6 tickets rule (BookingAgent)
def test_booking_agent_refuses_above_max_limit():
    async def run():
        db = TestingSessionLocal()
        cat = db.query(TicketCategory).first()
        u1 = db.query(User).filter(User.email == "alice@test.edu").first()
        res = await bus.send(ACLMessage(
            sender="Test", receiver="BookingAgent", performative=Performative.REQUEST,
            intent="book_tickets", content={"user_id": u1.id, "category_id": cat.id, "quantity": 7}
        ), db)
        assert res.performative == Performative.REFUSE
        assert "between 1 and 6" in res.content["reason"]
        db.close()

    asyncio.run(run())


# 3. Double Check-In Prevention (TicketAgent)
def test_ticket_agent_admits_once_and_rejects_second_scan():
    async def run():
        db = TestingSessionLocal()
        cat = db.query(TicketCategory).first()
        u1 = db.query(User).filter(User.email == "alice@test.edu").first()
        book = await bus.send(ACLMessage(
            sender="Test", receiver="BookingAgent", performative=Performative.REQUEST,
            intent="book_tickets", content={"user_id": u1.id, "category_id": cat.id, "quantity": 1}
        ), db)
        code = book.content["ticket_codes"][0]

        scan1 = await bus.send(ACLMessage(
            sender="Gate", receiver="TicketAgent", performative=Performative.REQUEST,
            intent="validate_scan", content={"event_id": cat.event_id, "code": code, "bypass_window": True}
        ), db)
        assert scan1.performative == Performative.AGREE
        assert scan1.content["verdict"] == "ADMIT"

        scan2 = await bus.send(ACLMessage(
            sender="Gate", receiver="TicketAgent", performative=Performative.REQUEST,
            intent="validate_scan", content={"event_id": cat.event_id, "code": code, "bypass_window": True}
        ), db)
        assert scan2.performative == Performative.REFUSE
        assert scan2.content["verdict"] == "ALREADY_USED"
        db.close()

    asyncio.run(run())


# 4. Wrong Event & Outside Check-In Time (TicketAgent)
def test_ticket_agent_wrong_event_and_outside_window():
    async def run():
        db = TestingSessionLocal()
        cat = db.query(TicketCategory).first()
        other_ev = db.query(Event).filter(Event.title == "Other Concert").first()
        u1 = db.query(User).filter(User.email == "alice@test.edu").first()
        book = await bus.send(ACLMessage(
            sender="Test", receiver="BookingAgent", performative=Performative.REQUEST,
            intent="book_tickets", content={"user_id": u1.id, "category_id": cat.id, "quantity": 1}
        ), db)
        code = book.content["ticket_codes"][0]

        # Wrong event
        wrong = await bus.send(ACLMessage(
            sender="Gate", receiver="TicketAgent", performative=Performative.REQUEST,
            intent="validate_scan", content={"event_id": other_ev.id, "code": code, "bypass_window": True}
        ), db)
        assert wrong.content["verdict"] == "WRONG_EVENT"

        # Outside time window (event is 3 days away)
        early = await bus.send(ACLMessage(
            sender="Gate", receiver="TicketAgent", performative=Performative.REQUEST,
            intent="validate_scan", content={"event_id": cat.event_id, "code": code, "bypass_window": False}
        ), db)
        assert early.content["verdict"] == "OUTSIDE_TIME"
        db.close()

    asyncio.run(run())


# 5. Suspicious Repeated Scan Detection (TicketAgent)
def test_ticket_agent_flags_suspicious_repeated_scans():
    async def run():
        db = TestingSessionLocal()
        cat = db.query(TicketCategory).first()
        u1 = db.query(User).filter(User.email == "alice@test.edu").first()
        book = await bus.send(ACLMessage(
            sender="Test", receiver="BookingAgent", performative=Performative.REQUEST,
            intent="book_tickets", content={"user_id": u1.id, "category_id": cat.id, "quantity": 1}
        ), db)
        code = book.content["ticket_codes"][0]
        for _ in range(3):
            res = await bus.send(ACLMessage(
                sender="Gate", receiver="TicketAgent", performative=Performative.REQUEST,
                intent="validate_scan", content={"event_id": cat.event_id, "code": code, "bypass_window": True}
            ), db)
        assert res.content["suspicious"] is True
        db.close()

    asyncio.run(run())


# 6. Cancellation Refund Policy & Cancelled Ticket Rejection
def test_cancellation_refund_and_ticket_voiding():
    async def run():
        db = TestingSessionLocal()
        cat = db.query(TicketCategory).first()
        u1 = db.query(User).filter(User.email == "alice@test.edu").first()
        book = await bus.send(ACLMessage(
            sender="Test", receiver="BookingAgent", performative=Performative.REQUEST,
            intent="book_tickets", content={"user_id": u1.id, "category_id": cat.id, "quantity": 1}
        ), db)
        cancel = await bus.send(ACLMessage(
            sender="Test", receiver="BookingAgent", performative=Performative.REQUEST,
            intent="cancel_booking", content={"booking_id": book.content["booking_id"]}
        ), db)
        assert cancel.performative == Performative.AGREE
        assert cancel.content["refund_amount"] == 20.0  # >=48h => 100%

        scan = await bus.send(ACLMessage(
            sender="Gate", receiver="TicketAgent", performative=Performative.REQUEST,
            intent="validate_scan", content={"event_id": cat.event_id, "code": book.content["ticket_codes"][0], "bypass_window": True}
        ), db)
        assert scan.content["verdict"] == "CANCELLED"
        db.close()

    asyncio.run(run())


# 7. Waitlist FIFO Offer & Proactive Timer Sweep (WaitlistAgent)
def test_waitlist_offer_and_proactive_expiry_sweep():
    async def run():
        db = TestingSessionLocal()
        cat = db.query(TicketCategory).first()
        u1 = db.query(User).filter(User.email == "alice@test.edu").first()
        u2 = db.query(User).filter(User.email == "bob@test.edu").first()

        # Alice books the only seat
        book = await bus.send(ACLMessage(
            sender="Test", receiver="BookingAgent", performative=Performative.REQUEST,
            intent="book_tickets", content={"user_id": u1.id, "category_id": cat.id, "quantity": 1}
        ), db)
        # Bob joins waitlist
        await bus.send(ACLMessage(
            sender="Test", receiver="WaitlistAgent", performative=Performative.REQUEST,
            intent="join_waitlist", content={"user_id": u2.id, "category_id": cat.id, "quantity": 1}
        ), db)
        # Alice cancels -> WaitlistAgent holds seat for Bob
        await bus.send(ACLMessage(
            sender="Test", receiver="BookingAgent", performative=Performative.REQUEST,
            intent="cancel_booking", content={"booking_id": book.content["booking_id"]}
        ), db)
        entry = db.query(WaitlistEntry).filter(WaitlistEntry.user_id == u2.id).first()
        assert entry.status == "OFFERED"

        # Simulate hold expiry and run proactive sweep
        entry.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        await waitlist_agent.proactive_expiry_sweep(db, bus)
        db.refresh(entry)
        assert entry.status == "EXPIRED"
        db.close()

    asyncio.run(run())


# 8. VenueAgent Turnaround Buffer & Counter-Proposal (PROPOSE)
def test_venue_agent_clash_returns_propose_with_alternatives():
    async def run():
        db = TestingSessionLocal()
        ev = db.query(Event).filter(Event.title == "Tech Fest").first()
        u1 = db.query(User).filter(User.email == "alice@test.edu").first()

        # Request overlaps Tech Fest
        res = await bus.send(ACLMessage(
            sender="Test", receiver="VenueAgent", performative=Performative.REQUEST,
            intent="request_private_hire",
            content={
                "user_id": u1.id, "venue_id": ev.venue_id,
                "start_time": ev.start_time.isoformat(),
                "end_time": (ev.start_time + timedelta(hours=2)).isoformat(),
                "purpose": "Club Meeting", "guest_count": 30
            }
        ), db)
        assert res.performative == Performative.PROPOSE
        assert len(res.content["alternatives"]) > 0
        db.close()

    asyncio.run(run())


# 9. VenueAgent Auto-Confirm vs Escalation
def test_venue_agent_escalation_for_large_hire():
    async def run():
        db = TestingSessionLocal()
        v2 = db.query(Venue).filter(Venue.name == "Hall B").first()
        u1 = db.query(User).filter(User.email == "alice@test.edu").first()
        start = datetime.now(timezone.utc) + timedelta(days=10)
        res = await bus.send(ACLMessage(
            sender="Test", receiver="VenueAgent", performative=Performative.REQUEST,
            intent="request_private_hire",
            content={
                "user_id": u1.id, "venue_id": v2.id,
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(hours=5)).isoformat(),  # > 4h triggers escalation
                "purpose": "Annual Gala", "guest_count": 120
            }
        ), db)
        assert res.performative == Performative.AGREE
        assert res.content["status"] == "ESCALATED"
        db.close()

    asyncio.run(run())


# 10. NotificationAgent Reactive Broadcast Listener
def test_notification_agent_creates_user_alert_on_broadcast():
    async def run():
        db = TestingSessionLocal()
        u1 = db.query(User).filter(User.email == "alice@test.edu").first()
        await bus.send(ACLMessage(
            sender="BookingAgent", receiver="BROADCAST", performative=Performative.INFORM,
            intent="booking_confirmed",
            content={"user_id": u1.id, "event_title": "Tech Fest", "quantity": 1, "payment_ref": "PAY-12345678"}
        ), db)
        notif = db.query(Notification).filter(Notification.user_id == u1.id).first()
        assert notif is not None
        assert "PAY-12345678" in notif.message
        db.close()

    asyncio.run(run())


# 11. HTTP Agent Inbox Endpoint (FastAPI + Swagger API)
def test_http_agent_inbox_endpoint():
    client = TestClient(app)
    db = TestingSessionLocal()
    v2 = db.query(Venue).filter(Venue.name == "Hall B").first()
    db.close()
    start = datetime.now(timezone.utc) + timedelta(days=12)
    resp = client.post("/api/agents/VenueAgent/inbox", json={
        "sender": "ExternalClient",
        "receiver": "VenueAgent",
        "performative": "REQUEST",
        "intent": "check_event_slot",
        "content": {"venue_id": v2.id, "start_time": start.isoformat(), "end_time": (start + timedelta(hours=2)).isoformat()}
    })
    assert resp.status_code == 200
    assert resp.json()["performative"] == "AGREE"


# 12. Security Role Enforcement (Unauthenticated user blocked from Admin & Scanner)
def test_rbac_blocks_unauthorized_access():
    client = TestClient(app)
    assert client.get("/admin").status_code == 401
    assert client.get("/scanner").status_code == 401