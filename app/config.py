import os
from dotenv import load_dotenv

# Loads variables from .env in local development; ignored safely in cloud hosts like Render
load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY environment variable is not set. Check your .env file.")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./eventsphere.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Demo account seed credentials from environment
DEMO_ADMIN_EMAIL = os.getenv("DEMO_ADMIN_EMAIL", "admin@eventsphere.edu")
DEMO_ADMIN_PASSWORD = os.getenv("DEMO_ADMIN_PASSWORD", "Password123!")
DEMO_STAFF_EMAIL = os.getenv("DEMO_STAFF_EMAIL", "staff@eventsphere.edu")
DEMO_STAFF_PASSWORD = os.getenv("DEMO_STAFF_PASSWORD", "Password123!")
DEMO_ATTENDEE_EMAIL = os.getenv("DEMO_ATTENDEE_EMAIL", "student@eventsphere.edu")
DEMO_ATTENDEE_PASSWORD = os.getenv("DEMO_ATTENDEE_PASSWORD", "Password123!")

# Agent Domain Constants
VENUE_TURNAROUND_MINUTES = 60
MAX_TICKETS_PER_BOOKING = 6
WAITLIST_HOLD_MINUTES = 10
SUSPICIOUS_SCAN_THRESHOLD = 3