import os
from sqlmodel import SQLModel, Session, create_engine

# Anchor relative SQLite paths to the project root, not the current working
# directory — otherwise running commands from different folders (e.g. `cd
# engine && python run.py` vs `uvicorn app.main:app` from the root) silently
# creates two separate database files instead of sharing one.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///local.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL.startswith("sqlite:///") and not DATABASE_URL.startswith("sqlite:////"):
    relative_path = DATABASE_URL[len("sqlite:///"):]
    absolute_path = os.path.join(_PROJECT_ROOT, relative_path)
    DATABASE_URL = f"sqlite:///{absolute_path}"

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def init_db():
    SQLModel.metadata.create_all(engine)


def get_session():
    return Session(engine)