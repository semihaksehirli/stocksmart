"""Database engine and session setup.

SQLite is used for local development; set DATABASE_URL to a PostgreSQL
URL (postgresql+psycopg://...) for deployment.
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./stocksmart.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db(bind=None) -> None:
    from app import models  # noqa: F401  (registers tables)

    Base.metadata.create_all(bind=bind or engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
