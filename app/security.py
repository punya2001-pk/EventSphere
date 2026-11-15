import secrets
from passlib.context import CryptContext
from fastapi import Request, HTTPException

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token

def validate_csrf(request: Request, form_token: str):
    session_token = request.session.get("csrf_token")
    if not session_token or not form_token or not secrets.compare_digest(session_token, form_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")