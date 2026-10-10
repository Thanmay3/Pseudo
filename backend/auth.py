import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import AuthSession, User

router = APIRouter(prefix="/auth", tags=["authentication"])
hasher = PasswordHasher()  # Established Argon2id implementation and random salts.
dummy_hash = hasher.hash(secrets.token_urlsafe(32))
COOKIE = "pseudo_session"
MAX_AGE = 7 * 24 * 60 * 60


def secure_cookie():
    return os.getenv("COOKIE_SECURE", "false").lower() == "true"


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        return str(value).lower()


class RegisterInput(LoginInput):
    password: str = Field(min_length=12, max_length=128)
    pseudonym: str = Field(min_length=3, max_length=30, pattern=r"^[A-Za-z0-9_]+$")


class AccountOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    pseudonym: str
    created_at: datetime


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(COOKIE)
    if not token or len(token) > 128:
        raise HTTPException(401, "Please log in")
    user = db.scalar(select(User).join(AuthSession, AuthSession.user_id == User.id).where(
        AuthSession.token_hash == token_hash(token), AuthSession.expires_at > datetime.now(timezone.utc)))
    if user is None:
        raise HTTPException(401, "Please log in")
    return user


def authenticate(user, request, response, db):
    old_token = request.cookies.get(COOKIE)
    if old_token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(old_token)))
    db.execute(delete(AuthSession).where(AuthSession.expires_at <= datetime.now(timezone.utc)))
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(token_hash=token_hash(token), user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=MAX_AGE)))
    db.commit()
    response.set_cookie(COOKIE, token, max_age=MAX_AGE, httponly=True,
        secure=secure_cookie(), samesite="lax", path="/")


@router.post("/register", status_code=201, response_model=AccountOutput)
def register(data: RegisterInput, request: Request, response: Response, db: Session = Depends(get_db)):
    user = User(email=str(data.email), pseudonym=data.pseudonym, password_hash=hasher.hash(data.password))
    db.add(user)
    try:
        db.flush()  # Database unique constraints also protect concurrent registrations.
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Email or pseudonym is already in use") from None
    authenticate(user, request, response, db)
    return user


@router.post("/login", response_model=AccountOutput)
def login(data: LoginInput, request: Request, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == str(data.email)))
    try:
        hasher.verify(user.password_hash if user else dummy_hash, data.password)
    except (VerificationError, InvalidHashError):
        raise HTTPException(401, "Invalid email or password") from None
    if user is None:
        raise HTTPException(401, "Invalid email or password")
    if hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hasher.hash(data.password)
    authenticate(user, request, response, db)
    return user


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(COOKIE)
    if token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(token)))
        db.commit()
    response.delete_cookie(COOKIE, path="/", httponly=True, secure=secure_cookie(), samesite="lax")
    return {"status": "ok"}


@router.get("/me", response_model=AccountOutput)
def me(user: User = Depends(current_user)):
    return user
