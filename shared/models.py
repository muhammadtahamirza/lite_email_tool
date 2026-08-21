"""
Shared database models. Both the FastAPI backend (Render) and the sending
engine (GitHub Actions) import this file, so there is exactly one definition
of the schema — no drift between what the dashboard writes and what the
engine reads.
"""

from datetime import datetime
from typing import Optional, List
from sqlmodel import SQLModel, Field, Column, JSON


class Mailbox(SQLModel, table=True):
    __tablename__ = "mailboxes"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    encrypted_password: str
    from_name: str = ""
    verified: bool = False
    active: bool = True  # can be toggled off without deleting (e.g. temporarily pause a mailbox)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Template(SQLModel, table=True):
    __tablename__ = "templates"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    kind: str  # "first_touch" or "followup"
    # For "first_touch" templates: subject is used directly.
    # For "followup" templates: subject is ignored — follow-ups always thread
    # as "Re: <original first-touch subject>" automatically.
    subject: str = ""
    body: str
    # Only relevant for kind="followup": order in the sequence (1, 2, 3...)
    # and how many days must pass after the previous send before this fires.
    step_order: Optional[int] = None
    gap_days: Optional[int] = None
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Contact(SQLModel, table=True):
    __tablename__ = "contacts"

    id: Optional[int] = Field(default=None, primary_key=True)
    business_name: str
    contact_email: str = Field(unique=True, index=True)
    first_touch_template_id: Optional[int] = Field(default=None, foreign_key="templates.id")
    extra_info: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SendLog(SQLModel, table=True):
    __tablename__ = "send_log"

    id: Optional[int] = Field(default=None, primary_key=True)
    contact_id: int = Field(index=True, foreign_key="contacts.id")
    contact_email: str = Field(index=True)
    business_name: str = ""
    template_id: Optional[int] = Field(default=None, foreign_key="templates.id")
    template_name: str = ""
    mailbox_id: Optional[int] = Field(default=None, foreign_key="mailboxes.id")
    mailbox_email: str = ""
    follow_up_number: int = 1
    date_sent: datetime = Field(default_factory=datetime.utcnow)
    replied: bool = False
    reply_date: Optional[datetime] = None
    interested: Optional[bool] = None
    trial_started: bool = False
    notes: str = ""

    # Threading
    subject: str = ""
    message_id: str = ""
    in_reply_to: Optional[str] = None
    references: Optional[str] = None
