# EventSphere

EventSphere is a multi-agent university event and ticketing management web application built with FastAPI, SQLAlchemy, SQLite, Jinja2, and Bootstrap 5.

## Features
- Role-based authentication (Attendee, Admin & Staff) with dedicated login and registration pages
- Multi-agent system architecture (Venue & Scheduling, Ticketing & Inventory, Waitlist & Promotion, Gate Check-In & Fraud, Notification & Audit)
- Event & Venue management enforcing a mandatory 60-minute turnaround buffer between events
- Ticket booking with capacity enforcement (max 6 per booking), automated FIFO waitlist promotion, QR code tickets, and booking history
- QR ticket scanning and check-in for gate staff with duplicate check-in prevention and suspicious scan alerting
- Live cloud database deployment on PythonAnywhere and automatic SQLite database initialization locally

## Requirements
- Python 3.10 or later
- pip
- SQLite (used automatically by default for local setup)

## Run locally

From the repository root, clone the project, create and activate a virtual environment, and install the dependencies.

1. Create a virtual environment (.venv)
python -m venv .venv

2. Activate the virtual environment (Windows)
.venv\Scripts\activate

3. Install all required Python packages
pip install -r requirements.txt

4. Start the local FastAPI development server
uvicorn app.main:app --reload
Create a .env file in the repository root and configure your environment variables. Generate a suitable secret key with:

python -c "import secrets; print(secrets.token_urlsafe(48))"

# Sample .env

SECRET_KEY=replace_with_a_generated_secret_at_least_32_characters_long
 DATABASE_URL="Database_URL_Here"
 ALGORITHM=""
 ACCESS_TOKEN_EXPIRE_MINUTES=""

Start the application:

uvicorn app.main:app --reload

Open in your browser:

App: http://127.0.0.1:8000/

API Documentation: http://127.0.0.1:8000/docs

## Environment variables

| Variable | Required | Default | Description |
| :--- | :---: | :--- | :--- |
| `SECRET_KEY` | Yes | *(Required)* | Secret key used to sign session cookies and CSRF tokens. |
| `DATABASE_URL` | No | `sqlite:///./eventsphere.db` | Database connection URL. |
| `DEMO_ADMIN_EMAIL` | No | `admin@eventsphere.edu` | Email address seeded for the default Admin account. |
| `DEMO_ADMIN_PASSWORD` | No | `Password123!` | Password seeded for the default Admin account. |
| `DEMO_STAFF_EMAIL` | No | `staff@eventsphere.edu` | Email address seeded for the default Gate Staff account. |
| `DEMO_STAFF_PASSWORD` | No | `Password123!` | Password seeded for the default Gate Staff account. |
| `DEMO_ATTENDEE_EMAIL` | No | `student@eventsphere.edu` | Email address seeded for the default Attendee account. |
| `DEMO_ATTENDEE_PASSWORD` | No | `Password123!` | Password seeded for the default Attendee account. |

**Live URL:** https://punya123.pythonanywhere.com   

## Demo Account 

Admin:
Email - admin@eventsphere.edu
Password - Password123!
     
Staff: 
Email - staff@eventsphere.edu
Password - Password123!
     
Attendee:
Email - student@eventsphere.edu
Password - Password123!



        
