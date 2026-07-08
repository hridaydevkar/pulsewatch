"""
database.py
Sets up the SQLite database engine and session for the app.
Using SQLite keeps this project zero-config: no external DB server needed
to clone and run it, which matters a lot for a portfolio project.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "sqlite:///./uptime.db"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
