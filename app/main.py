import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session

from app.config import (
    SECRET_KEY,
    DEMO_ADMIN_EMAIL, DEMO_ADMIN_PASSWORD,
    DEMO_STAFF_EMAIL, DEMO_STAFF_PASSWORD,
    DEMO_ATTENDEE_EMAIL, DEMO_ATTENDEE_PASSWORD,
)
from app.database import engine, Base, get_db, SessionLocal
from app.models import (
    User, Venue, Event, TicketCategory, Booking, Ticket,
    VenueHireRequest, WaitlistEntry, Notification, AgentMessageLog, AgentBelief
)
from app.security import hash_password, verify_password, get_csrf_token, validate_csrf
from app.qr_utils import generate_qr_base64
from app.agents.protocol import ACLMessage, Performative
from app.agents.bus import bus
from app.agents.venue_agent import VenueAgent
from app.agents.booking_agent import BookingAgent
from app.agents.ticket_agent import TicketAgent
from app.agents.waitlist_agent import WaitlistAgent
from app.agents.notification_agent import NotificationAgent

# Instantiate and register the 5 autonomous agents
venue_agent = VenueAgent()
booking_agent = BookingAgent()
ticket_agent = TicketAgent()
waitlist_agent = WaitlistAgent()
notification_agent = NotificationAgent()

bus.register(venue_agent)
bus.register(booking_agent)
bus.register(ticket_agent)
bus.register(waitlist_agent)
bus.register(notification_agent, subscribe_broadcast=True)


def seed_database():
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            users = [
                User(
                    name="Dr. Perera (Admin)",
                    email=DEMO_ADMIN_EMAIL,
                    password_hash=hash_password(DEMO_ADMIN_PASSWORD),
                    role="admin",
                ),
                User(
                    name="Kasun (Gate Staff)",
                    email=DEMO_STAFF_EMAIL,
                    password_hash=hash_password(DEMO_STAFF_PASSWORD),
                    role="staff",
                ),
                User(
                    name="Nimali (Student)",
                    email=DEMO_ATTENDEE_EMAIL,
                    password_hash=hash_password(DEMO_ATTENDEE_PASSWORD),
                    role="attendee",
                ),
            ]
            db.add_all(users)
            db.commit()

        if db.query(Venue).count() == 0:
            v1 = Venue(
                name="Main University Auditorium", location="Block A, Central Campus",
                capacity=300, description="Tiered acoustic hall with stage lighting and AV.",
                image_url="https://images.unsplash.com/photo-1517457373958-b7bdd4587205?w=800",
                hourly_rate=150.0, open_for_private_hire=True
            )
            v2 = Venue(
                name="Innovation Lab & Seminar Hall", location="IT Faculty, Floor 2",
                capacity=80, description="Modern modular seminar space for workshops and hackathons.",
                image_url="https://images.unsplash.com/photo-1497366216548-37526070297c?w=800",
                hourly_rate=60.0, open_for_private_hire=True
            )
            v3 = Venue(
                name="Open-Air Amphitheatre", location="Lakeside Grounds",
                capacity=500, description="Outdoor concert and cultural festival stage.",
                image_url="https://images.unsplash.com/photo-1501281668745-f7f57925c3b4?w=800",
                hourly_rate=200.0, open_for_private_hire=False
            )
            db.add_all([v1, v2, v3])
            db.commit()

            now = datetime.now(timezone.utc)
            e1 = Event(
                title="Annual Campus Music & Cultural Night", category="Concert", venue_id=v1.id,
                start_time=now + timedelta(days=3, hours=2), end_time=now + timedelta(days=3, hours=6),
                description="Live student bands, soloists, and guest performers.", status="published"
            )
            e2 = Event(
                title="Multi-Agent AI Systems Workshop", category="Workshop", venue_id=v2.id,
                start_time=now + timedelta(hours=1), end_time=now + timedelta(hours=5),
                description="Hands-on session building autonomous Python & FastAPI agents (Open for check-in now!).", status="published"
            )
            db.add_all([e1, e2])
            db.commit()

            db.add_all([
                TicketCategory(event_id=e1.id, name="Student Pass", price=10.0, total_quantity=200),
                TicketCategory(event_id=e1.id, name="VIP Front Row", price=25.0, total_quantity=50),
                TicketCategory(event_id=e2.id, name="Standard Seat", price=5.0, total_quantity=40),
            ])
            db.commit()
    finally:
        db.close()


async def waitlist_background_worker():
    """Proactive agent timer loop running every 15 seconds without user input."""
    while True:
        await asyncio.sleep(15)
        db = SessionLocal()
        try:
            await waitlist_agent.proactive_expiry_sweep(db, bus)
        except Exception:
            pass
        finally:
            db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    seed_database()
    task = asyncio.create_task(waitlist_background_worker())
    yield
    task.cancel()


app = FastAPI(title="EventSphere Multi-Agent Platform", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
templates = Jinja2Templates(directory="app/templates")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    uid = request.session.get("user_id")
    if not uid:
        return None
    return db.query(User).filter(User.id == uid).first()


def require_role(user: Optional[User], roles: list[str]):
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in first.")
    if user.role not in roles:
        raise HTTPException(status_code=403, detail=f"Access denied. Required role: {', '.join(roles)}")


def ctx(request: Request, db: Session, user: Optional[User], **kwargs):
    unread = 0
    notifs = []
    if user:
        notifs = (
            db.query(Notification)
            .filter(Notification.user_id == user.id)
            .order_by(Notification.created_at.desc())
            .limit(8)
            .all()
        )
        unread = sum(1 for n in notifs if not n.is_read)
    return {
        "user": user,
        "csrf_token": get_csrf_token(request),
        "notifications": notifs,
        "unread_count": unread,
        "flash": request.session.pop("flash", None),
        **kwargs,
    }


# ==================== PUBLIC & ATTENDEE ROUTES ====================

@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request, q: str = "", category: str = "", venue_id: str = "", max_price: str = "",
    db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)
):
    query = db.query(Event).filter(Event.status == "published")
    if q:
        query = query.filter(Event.title.ilike(f"%{q}%") | Event.description.ilike(f"%{q}%"))
    if category:
        query = query.filter(Event.category == category)
    if venue_id and venue_id.isdigit():
        query = query.filter(Event.venue_id == int(venue_id))

    events = query.order_by(Event.start_time.asc()).all()
    if max_price:
        try:
            mp = float(max_price)
            events = [e for e in events if any(c.price <= mp for c in e.categories)]
        except ValueError:
            pass

    venues = db.query(Venue).all()
    return templates.TemplateResponse(request, "index.html", ctx(
        request, db, user, events=events, venues=venues,
        q=q, category=category, venue_id=venue_id, max_price=max_price
    ))


@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)):
    return templates.TemplateResponse(request, "auth.html", ctx(request, db, user))


@app.post("/auth/login")
async def login(
    request: Request, email: str = Form(...), password: str = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db)
):
    validate_csrf(request, csrf_token)
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if not user or not verify_password(password, user.password_hash):
        request.session["flash"] = {"type": "danger", "text": "Invalid email or password."}
        return RedirectResponse("/auth", status_code=303)
    request.session["user_id"] = user.id
    request.session["flash"] = {"type": "success", "text": f"Welcome back, {user.name}!"}
    if user.role == "admin":
        return RedirectResponse("/admin", status_code=303)
    if user.role == "staff":
        return RedirectResponse("/scanner", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.post("/auth/register")
async def register(
    request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...),
    csrf_token: str = Form(...), db: Session = Depends(get_db)
):
    validate_csrf(request, csrf_token)
    clean_email = email.strip().lower()
    if db.query(User).filter(User.email == clean_email).first():
        request.session["flash"] = {"type": "danger", "text": "Email already registered."}
        return RedirectResponse("/auth", status_code=303)
    new_user = User(name=name.strip(), email=clean_email, password_hash=hash_password(password), role="attendee")
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    request.session["user_id"] = new_user.id
    request.session["flash"] = {"type": "success", "text": "Account created!"}
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.post("/notifications/read")
async def mark_notifications_read(request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)):
    if user:
        db.query(Notification).filter(
            Notification.user_id == user.id,
            Notification.is_read == False
        ).update({"is_read": True})
        db.commit()
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


@app.get("/events/{event_id}", response_class=HTMLResponse)
async def event_detail(
    event_id: int, request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)
):
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    active_holds = []
    if user:
        active_holds = db.query(WaitlistEntry).filter(
            WaitlistEntry.user_id == user.id,
            WaitlistEntry.event_id == event_id,
            WaitlistEntry.status.in_(["WAITING", "OFFERED"])
        ).all()
    return templates.TemplateResponse(request, "event_detail.html", ctx(
        request, db, user, event=event, active_holds=active_holds
    ))


@app.post("/events/{event_id}/book")
async def book_tickets(
    event_id: int, request: Request, category_id: int = Form(...), quantity: int = Form(...),
    waitlist_id: Optional[str] = Form(None), csrf_token: str = Form(...),
    db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)
):
    validate_csrf(request, csrf_token)
    require_role(user, ["attendee", "admin"])

    msg = ACLMessage(
        sender="WebGateway",
        receiver="BookingAgent",
        performative=Performative.REQUEST,
        intent="book_tickets",
        content={
            "user_id": user.id,
            "category_id": category_id,
            "quantity": quantity,
            "waitlist_id": int(waitlist_id) if waitlist_id and waitlist_id.isdigit() else None,
        }
    )
    reply = await bus.send(msg, db)
    if reply.performative == Performative.AGREE:
        request.session["flash"] = {
            "type": "success",
            "text": f"Booking confirmed! Ref: {reply.content['payment_ref']} (${reply.content['total_paid']:.2f})."
        }
        return RedirectResponse("/my-bookings", status_code=303)

    request.session["flash"] = {"type": "danger", "text": f"BookingAgent Refused: {reply.content.get('reason')}"}
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@app.post("/events/{event_id}/waitlist")
async def join_waitlist(
    event_id: int, request: Request, category_id: int = Form(...), quantity: int = Form(1),
    csrf_token: str = Form(...), db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)
):
    validate_csrf(request, csrf_token)
    require_role(user, ["attendee", "admin"])

    reply = await bus.send(ACLMessage(
        sender="WebGateway",
        receiver="WaitlistAgent",
        performative=Performative.REQUEST,
        intent="join_waitlist",
        content={"user_id": user.id, "category_id": category_id, "quantity": quantity}
    ), db)
    request.session["flash"] = {
        "type": "info" if reply.performative == Performative.AGREE else "danger",
        "text": reply.content.get("message") or reply.content.get("reason")
    }
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@app.get("/my-bookings", response_class=HTMLResponse)
async def my_bookings(request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)):
    require_role(user, ["attendee", "admin"])
    bookings = db.query(Booking).filter(Booking.user_id == user.id).order_by(Booking.created_at.desc()).all()
    qr_map = {}
    for b in bookings:
        for t in b.tickets:
            qr_map[t.id] = generate_qr_base64(t.code)
    waitlists = db.query(WaitlistEntry).filter(WaitlistEntry.user_id == user.id).order_by(WaitlistEntry.created_at.desc()).all()
    return templates.TemplateResponse(request, "my_bookings.html", ctx(
        request, db, user, bookings=bookings, qr_map=qr_map, waitlists=waitlists
    ))


@app.post("/bookings/{booking_id}/cancel")
async def cancel_booking(
    booking_id: int, request: Request, csrf_token: str = Form(...),
    db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)
):
    validate_csrf(request, csrf_token)
    require_role(user, ["attendee", "admin"])
    booking = db.query(Booking).filter(Booking.id == booking_id, Booking.user_id == user.id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    reply = await bus.send(ACLMessage(
        sender="WebGateway",
        receiver="BookingAgent",
        performative=Performative.REQUEST,
        intent="cancel_booking",
        content={"booking_id": booking.id}
    ), db)
    if reply.performative == Performative.AGREE:
        request.session["flash"] = {
            "type": "warning",
            "text": f"Cancelled. Refund: ${reply.content['refund_amount']:.2f} ({reply.content['policy_note']})."
        }
    else:
        request.session["flash"] = {"type": "danger", "text": reply.content.get("reason")}
    return RedirectResponse("/my-bookings", status_code=303)


# ==================== PRIVATE VENUE HIRE ====================

@app.get("/venue-hire", response_class=HTMLResponse)
async def venue_hire_page(request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)):
    require_role(user, ["attendee", "admin"])
    venues = db.query(Venue).filter(Venue.open_for_private_hire == True).all()
    my_hires = db.query(VenueHireRequest).filter(VenueHireRequest.user_id == user.id).order_by(VenueHireRequest.created_at.desc()).all()
    proposal = request.session.pop("hire_proposal", None)
    return templates.TemplateResponse(request, "venue_hire.html", ctx(
        request, db, user, venues=venues, my_hires=my_hires, proposal=proposal
    ))


@app.post("/venue-hire")
async def submit_venue_hire(
    request: Request, venue_id: int = Form(...), start_time: str = Form(...), end_time: str = Form(...),
    purpose: str = Form(...), guest_count: int = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)
):
    validate_csrf(request, csrf_token)
    require_role(user, ["attendee", "admin"])

    reply = await bus.send(ACLMessage(
        sender="WebGateway",
        receiver="VenueAgent",
        performative=Performative.REQUEST,
        intent="request_private_hire",
        content={
            "user_id": user.id,
            "venue_id": venue_id,
            "start_time": start_time,
            "end_time": end_time,
            "purpose": purpose,
            "guest_count": guest_count,
        }
    ), db)

    if reply.performative == Performative.AGREE:
        request.session["flash"] = {
            "type": "success",
            "text": f"Venue Hire {reply.content['status']}: ${reply.content['quoted_price']:.2f} ({reply.content['message']})"
        }
    elif reply.performative == Performative.PROPOSE:
        request.session["flash"] = {"type": "warning", "text": f"VenueAgent Counter-Proposal: {reply.content['reason']}"}
        request.session["hire_proposal"] = {
            "reason": reply.content["reason"],
            "alternatives": reply.content.get("alternatives", []),
            "purpose": purpose,
            "guest_count": guest_count,
        }
    else:
        request.session["flash"] = {"type": "danger", "text": f"VenueAgent Refused: {reply.content.get('reason')}"}

    return RedirectResponse("/venue-hire", status_code=303)


# ==================== GATE STAFF SCANNER ====================

@app.get("/scanner", response_class=HTMLResponse)
async def scanner_page(
    request: Request, event_id: Optional[int] = None, db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user)
):
    require_role(user, ["staff", "admin"])
    events = db.query(Event).filter(Event.status == "published").all()
    selected_event_id = event_id or (events[0].id if events else 0)
    scan_result = request.session.pop("scan_result", None)
    return templates.TemplateResponse(request, "scanner.html", ctx(
        request, db, user, events=events, selected_event_id=selected_event_id, scan_result=scan_result
    ))


@app.post("/scanner/verify")
async def verify_ticket_scan(
    request: Request, event_id: int = Form(...), code: str = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)
):
    validate_csrf(request, csrf_token)
    require_role(user, ["staff", "admin"])

    reply = await bus.send(ACLMessage(
        sender="GateScannerUI",
        receiver="TicketAgent",
        performative=Performative.REQUEST,
        intent="validate_scan",
        content={"event_id": event_id, "code": code}
    ), db)

    request.session["scan_result"] = {
        "admitted": reply.performative == Performative.AGREE,
        "verdict": reply.content.get("verdict"),
        "reason": reply.content.get("reason"),
        "suspicious": reply.content.get("suspicious", False),
        "code": code,
    }
    return RedirectResponse(f"/scanner?event_id={event_id}", status_code=303)


# ==================== ADMIN / ORGANISER DASHBOARD ====================

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request, conv_id: str = "", db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user)
):
    require_role(user, ["admin"])
    venues = db.query(Venue).all()
    events = db.query(Event).order_by(Event.start_time.desc()).all()
    escalated_hires = db.query(VenueHireRequest).filter(VenueHireRequest.status == "ESCALATED").all()

    event_stats = []
    for ev in events:
        total_cap = sum(c.total_quantity for c in ev.categories)
        sold = sum(c.sold_quantity for c in ev.categories)
        rev = sum(c.sold_quantity * c.price for c in ev.categories)
        checked_in = db.query(Ticket).filter(Ticket.event_id == ev.id, Ticket.status == "USED").count()
        event_stats.append({
            "event": ev,
            "venue_name": ev.venue.name if ev.venue else "Unknown",
            "capacity": total_cap,
            "sold": sold,
            "revenue": rev,
            "checked_in": checked_in,
        })

    chart_labels = [st["event"].title[:20] for st in event_stats]
    chart_sold = [st["sold"] for st in event_stats]
    chart_checked_in = [st["checked_in"] for st in event_stats]

    logs_q = db.query(AgentMessageLog).order_by(AgentMessageLog.id.desc())
    if conv_id:
        logs_q = logs_q.filter(AgentMessageLog.conversation_id == conv_id.strip())
    logs = logs_q.limit(60).all()
    beliefs = db.query(AgentBelief).all()

    return templates.TemplateResponse(request, "admin_dashboard.html", ctx(
        request, db, user,
        venues=venues,
        events=events,
        escalated_hires=escalated_hires,
        event_stats=event_stats,
        chart_labels=chart_labels,
        chart_sold=chart_sold,
        chart_checked_in=chart_checked_in,
        logs=logs,
        beliefs=beliefs,
        conv_id=conv_id,
    ))


@app.post("/admin/venues")
async def create_or_edit_venue(
    request: Request, venue_id: Optional[str] = Form(None), name: str = Form(...), location: str = Form(...),
    capacity: int = Form(...), hourly_rate: float = Form(...), description: str = Form(""),
    image_url: str = Form(""), open_for_private_hire: Optional[str] = Form(None),
    csrf_token: str = Form(...), db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)
):
    validate_csrf(request, csrf_token)
    require_role(user, ["admin"])
    is_open = open_for_private_hire == "on"
    img = image_url.strip() or "https://images.unsplash.com/photo-1517457373958-b7bdd4587205?w=800"

    if venue_id and venue_id.isdigit():
        v = db.query(Venue).filter(Venue.id == int(venue_id)).first()
        if v:
            v.name, v.location, v.capacity, v.hourly_rate = name, location, capacity, hourly_rate
            v.description, v.image_url, v.open_for_private_hire = description, img, is_open
    else:
        db.add(Venue(
            name=name, location=location, capacity=capacity, hourly_rate=hourly_rate,
            description=description, image_url=img, open_for_private_hire=is_open
        ))
    db.commit()
    request.session["flash"] = {"type": "success", "text": "Venue saved."}
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/events")
async def create_event(
    request: Request, title: str = Form(...), category: str = Form(...), venue_id: int = Form(...),
    start_time: str = Form(...), end_time: str = Form(...), description: str = Form(""),
    cat_name: str = Form(...), cat_price: float = Form(...), cat_qty: int = Form(...),
    action: str = Form("publish"), csrf_token: str = Form(...),
    db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)
):
    validate_csrf(request, csrf_token)
    require_role(user, ["admin"])

    venue = db.query(Venue).filter(Venue.id == venue_id).first()
    if not venue or cat_qty > venue.capacity or cat_qty < 1:
        request.session["flash"] = {
            "type": "danger",
            "text": f"Ticket quantity ({cat_qty}) must be between 1 and venue capacity ({venue.capacity if venue else 0})."
        }
        return RedirectResponse("/admin", status_code=303)

    reply = await bus.send(ACLMessage(
        sender="AdminUI",
        receiver="VenueAgent",
        performative=Performative.REQUEST,
        intent="check_event_slot",
        content={"venue_id": venue_id, "start_time": start_time, "end_time": end_time}
    ), db)

    if reply.performative == Performative.REFUSE:
        request.session["flash"] = {"type": "danger", "text": f"VenueAgent Refused: {reply.content.get('reason')}"}
        return RedirectResponse("/admin", status_code=303)

    s_dt = datetime.fromisoformat(start_time)
    e_dt = datetime.fromisoformat(end_time)
    if s_dt.tzinfo is None:
        s_dt = s_dt.replace(tzinfo=timezone.utc)
        e_dt = e_dt.replace(tzinfo=timezone.utc)

    ev = Event(
        title=title, category=category, venue_id=venue_id, start_time=s_dt, end_time=e_dt,
        description=description, status="published" if action == "publish" else "draft"
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    db.add(TicketCategory(event_id=ev.id, name=cat_name, price=cat_price, total_quantity=cat_qty))
    db.commit()

    request.session["flash"] = {"type": "success", "text": f"Event '{title}' created ({ev.status})."}
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/events/{event_id}/status")
async def change_event_status(
    event_id: int, request: Request, new_status: str = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)
):
    validate_csrf(request, csrf_token)
    require_role(user, ["admin"])
    ev = db.query(Event).filter(Event.id == event_id).first()
    if not ev:
        raise HTTPException(status_code=404)

    if new_status == "published":
        reply = await bus.send(ACLMessage(
            sender="AdminUI", receiver="VenueAgent", performative=Performative.REQUEST,
            intent="check_event_slot",
            content={
                "venue_id": ev.venue_id,
                "start_time": ev.start_time.isoformat(),
                "end_time": ev.end_time.isoformat(),
                "ignore_event_id": ev.id,
            }
        ), db)
        if reply.performative == Performative.REFUSE:
            request.session["flash"] = {"type": "danger", "text": reply.content.get("reason")}
            return RedirectResponse("/admin", status_code=303)
        ev.status = "published"
        db.commit()

    elif new_status == "cancelled":
        ev.status = "cancelled"
        db.commit()
        await bus.send(ACLMessage(
            sender="AdminUI", receiver="BookingAgent", performative=Performative.REQUEST,
            intent="event_cancelled", content={"event_id": ev.id}
        ), db)

    request.session["flash"] = {"type": "info", "text": f"Event status updated to {new_status}."}
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/hires/{hire_id}/decide")
async def decide_hire(
    hire_id: int, request: Request, decision: str = Form(...), csrf_token: str = Form(...),
    db: Session = Depends(get_db), user: Optional[User] = Depends(get_current_user)
):
    validate_csrf(request, csrf_token)
    require_role(user, ["admin"])
    hire = db.query(VenueHireRequest).filter(VenueHireRequest.id == hire_id).first()
    if hire:
        hire.status = "CONFIRMED" if decision == "approve" else "REJECTED"
        venue_name = hire.venue.name if hire.venue else "Venue"
        conv_id = hire.conversation_id or f"conv-hire-{hire.id}"
        db.commit()
        await bus.send(ACLMessage(
            conversation_id=conv_id, sender="VenueAgent", receiver="BROADCAST",
            performative=Performative.INFORM, intent="venue_hire_updated",
            content={
                "user_id": hire.user_id, "venue_name": venue_name,
                "status": hire.status, "quoted_price": hire.quoted_price, "hire_id": hire.id
            }
        ), db)
    return RedirectResponse("/admin", status_code=303)


# ==================== HTTP AGENT INBOX API (PART C + SWAGGER DOCS) ====================

@app.post("/api/agents/{agent_name}/inbox", response_model=ACLMessage, tags=["Multi-Agent HTTP Inboxes"])
async def agent_http_inbox(agent_name: str, message: ACLMessage, db: Session = Depends(get_db)):
    """Direct HTTP ACLMessage endpoint for any agent (VenueAgent, BookingAgent, TicketAgent, WaitlistAgent, NotificationAgent)."""
    message.receiver = agent_name
    return await bus.send(message, db)