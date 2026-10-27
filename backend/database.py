from pathlib import Path
from dotenv import load_dotenv
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL or "YOUR_PASSWORD" in DATABASE_URL:
    raise RuntimeError("Set your real DATABASE_URL in backend/.env or the project-root .env")

if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass
