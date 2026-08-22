import sys
import os
import csv
import io
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import select

from shared.db import init_db, get_session
from shared.models import Mailbox, Contact, Template, SendLog
from shared.crypto import encrypt_password
from app.auth import require_auth

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import mailer
import sheets_sync

app = FastAPI(title="Outreach Dashboard API")


@app.on_event("startup")
def on_startup():
    init_db()


# ---------- Schemas ----------

class MailboxIn(BaseModel):
    email: str
    password: str
    from_name: str = ""


class TemplateIn(BaseModel):
    name: str
    kind: str  # "first_touch" or "followup"
    subject: str = ""
    body: str
    step_order: Optional[int] = None
    gap_days: Optional[int] = None


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    step_order: Optional[int] = None
    gap_days: Optional[int] = None
    active: Optional[bool] = None


# ---------- Mailboxes ----------

@app.post("/api/mailboxes", dependencies=[Depends(require_auth)])
def add_mailbox(payload: MailboxIn):
    """Verifies via a real IMAP login BEFORE saving anything. This live
    login attempt is the confirmation step — no separate 'confirm' action
    is needed since a successful login already proves the credentials work."""
    success, message = mailer.verify_mailbox(payload.email, payload.password)
    if not success:
        raise HTTPException(status_code=400, detail=f"Mailbox verification failed: {message}")

    session = get_session()
    existing = session.exec(select(Mailbox).where(Mailbox.email == payload.email)).first()
    if existing:
        session.close()
        raise HTTPException(status_code=400, detail="This mailbox is already added.")

    mailbox = Mailbox(
        email=payload.email,
        encrypted_password=encrypt_password(payload.password),
        from_name=payload.from_name,
        verified=True,
    )
    session.add(mailbox)
    session.commit()
    session.refresh(mailbox)
    session.close()
    return {"id": mailbox.id, "email": mailbox.email, "verified": True, "message": "Mailbox added and verified."}


@app.get("/api/mailboxes", dependencies=[Depends(require_auth)])
def list_mailboxes():
    session = get_session()
    mailboxes = session.exec(select(Mailbox)).all()
    session.close()
    return [
        {"id": m.id, "email": m.email, "from_name": m.from_name, "verified": m.verified,
         "active": m.active, "created_at": m.created_at.isoformat()}
        for m in mailboxes
    ]


@app.delete("/api/mailboxes/{mailbox_id}", dependencies=[Depends(require_auth)])
def delete_mailbox(mailbox_id: int):
    session = get_session()
    mailbox = session.get(Mailbox, mailbox_id)
    if not mailbox:
        session.close()
        raise HTTPException(status_code=404, detail="Mailbox not found")
    session.delete(mailbox)
    session.commit()
    session.close()
    return {"deleted": True}


# ---------- Templates ----------

@app.post("/api/templates", dependencies=[Depends(require_auth)])
def create_template(payload: TemplateIn):
    if payload.kind not in ("first_touch", "followup"):
        raise HTTPException(status_code=400, detail="kind must be 'first_touch' or 'followup'")
    session = get_session()
    template = Template(**payload.dict())
    session.add(template)
    session.commit()
    session.refresh(template)
    session.close()
    return template


@app.get("/api/templates", dependencies=[Depends(require_auth)])
def list_templates():
    session = get_session()
    templates = session.exec(select(Template)).all()
    session.close()
    return templates


@app.put("/api/templates/{template_id}", dependencies=[Depends(require_auth)])
def update_template(template_id: int, payload: TemplateUpdate):
    session = get_session()
    template = session.get(Template, template_id)
    if not template:
        session.close()
        raise HTTPException(status_code=404, detail="Template not found")
    for key, value in payload.dict(exclude_unset=True).items():
        setattr(template, key, value)
    session.add(template)
    session.commit()
    session.refresh(template)
    session.close()
    return template


@app.delete("/api/templates/{template_id}", dependencies=[Depends(require_auth)])
def delete_template(template_id: int):
    session = get_session()
    template = session.get(Template, template_id)
    if not template:
        session.close()
        raise HTTPException(status_code=404, detail="Template not found")
    session.delete(template)
    session.commit()
    session.close()
    return {"deleted": True}


# ---------- Contacts / leads ----------

@app.post("/api/contacts/upload-csv", dependencies=[Depends(require_auth)])
async def upload_contacts_csv(file: UploadFile = File(...)):
    """Expected columns: business_name, contact_email, [first_touch_template
    (optional — name of a template; if omitted, auto-rotates across active
    first-touch templates)], plus any extra columns become personalization
    fields automatically (e.g. a 'city' column becomes {{city}})."""
    content = (await file.read()).decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))

    session = get_session()
    templates_by_name = {t.name: t for t in session.exec(select(Template)).all()}

    added, skipped, errors = 0, 0, []
    for row in reader:
        email = (row.get("contact_email") or "").strip().lower()
        business_name = (row.get("business_name") or "").strip()
        if not email or not business_name:
            errors.append(f"Skipped row missing business_name/contact_email: {row}")
            continue

        existing = session.exec(select(Contact).where(Contact.contact_email == email)).first()
        if existing:
            skipped += 1
            continue

        template_id = None
        template_name = (row.get("first_touch_template") or "").strip()
        if template_name:
            template = templates_by_name.get(template_name)
            if template:
                template_id = template.id
            else:
                errors.append(f"Unknown template '{template_name}' for {email} — will auto-rotate instead")

        extra_info = {
            k: v for k, v in row.items()
            if k not in ("business_name", "contact_email", "first_touch_template") and v
        }

        contact = Contact(
            business_name=business_name, contact_email=email,
            first_touch_template_id=template_id, extra_info=extra_info,
        )
        session.add(contact)
        added += 1

    session.commit()
    session.close()
    return {"added": added, "skipped_duplicates": skipped, "warnings": errors}


@app.get("/api/contacts", dependencies=[Depends(require_auth)])
def list_contacts(limit: int = 200):
    session = get_session()
    contacts = session.exec(select(Contact).order_by(Contact.created_at.desc()).limit(limit)).all()
    session.close()
    return contacts

@app.post("/api/contacts/pull-from-sheet", dependencies=[Depends(require_auth)])
def pull_from_sheet():
    if not os.getenv("GOOGLE_SHEET_ID"):
        raise HTTPException(status_code=400, detail="GOOGLE_SHEET_ID is not configured.")
    try:
        added = sheets_sync.pull_leads()
        return {"added": added}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Sheet pull failed: {e}")





# ---------- Stats ----------

@app.get("/api/stats", dependencies=[Depends(require_auth)])
def get_stats():
    session = get_session()
    logs = session.exec(select(SendLog)).all()
    session.close()

    total = len(logs)
    replied = sum(1 for l in logs if l.replied)
    interested = sum(1 for l in logs if l.interested)
    trials = sum(1 for l in logs if l.trial_started)

    by_template = {}
    for l in logs:
        t = l.template_name or "unspecified"
        by_template.setdefault(t, {"sent": 0, "replied": 0})
        by_template[t]["sent"] += 1
        if l.replied:
            by_template[t]["replied"] += 1

    for t, v in by_template.items():
        v["reply_rate"] = round((v["replied"] / v["sent"] * 100), 1) if v["sent"] else 0.0

    return {
        "total_sent": total,
        "replied": replied,
        "interested": interested,
        "trials_started": trials,
        "reply_rate": round((replied / total * 100), 1) if total else 0.0,
        "by_template": by_template,
    }


# ---------- Serve the dashboard frontend ----------

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def serve_dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
