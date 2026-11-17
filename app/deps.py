"""Shared FastAPI dependencies: the logged-in user and role checks."""
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app import auth
from app.db import get_db
from app.models import Role, User

bearer = HTTPBearer(auto_error=False)


def current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer), db: Session = Depends(get_db)
) -> User:
    data = auth.decode_token(creds.credentials) if creds else None
    user = db.get(User, data["sub"]) if data else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return user


def owner_only(user: User = Depends(current_user)) -> User:
    if user.role != Role.OWNER.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner role required")
    return user
