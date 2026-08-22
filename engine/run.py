#!/usr/bin/env python3
"""
The sending engine. Runs from GitHub Actions on a schedule. Reads
everything — mailboxes, contacts, templates — from Neon (populated via the
Render dashboard), so there is nothing to configure here beyond DATABASE_URL
and ENCRYPTION_KEY.

COMMANDS
--------
python run.py check --days 1        # scan all mailboxes for replies
python run.py run-daily --limit 80  # check -> followup -> send, in that order
"""

import sys
import os
import re
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from sqlmodel import select
from shared.db import init_db, get_session
from shared.models import Mailbox, Contact, Template, SendLog
from shared.crypto import decrypt_password
from shared.utils import utcnow
import mailer
import sheets_sync

DAILY_SEND_CAP = int(os.getenv("DAILY_SEND_CAP", "80"))


import re

def _render(text, fields):
    def match_handler(match):
        tag_content = match.group(1)

        if "|" in tag_content:
            key, fallback = tag_content.split("|", 1)
        else:
            key, fallback = tag_content, ""

        key = key.strip()
        fallback = fallback.strip()

        val = fields.get(key)

        if val is not None and str(val).strip() != "":
            return str(val).strip()

        return fallback

    return re.sub(r'\{\{(.*?)\}\}', match_handler, text)


def _build_merge_fields(contact: Contact):
    fields = {"business_name": contact.business_name}
    fields.update(contact.extra_info or {})
    return fields


def _active_mailboxes(session):
    return session.exec(select(Mailbox).where(Mailbox.active == True, Mailbox.verified == True)).all()


def _mailbox_for_contact(mailboxes, contact_id):
    """Deterministic per-contact assignment — same contact always gets the
    same mailbox for their whole thread, so a follow-up never appears to
    come from a different sender mid-conversation."""
    return mailboxes[contact_id % len(mailboxes)]


def _sent_today_count(session):
    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return len(session.exec(select(SendLog).where(SendLog.date_sent >= today_start)).all())


def _followup_templates(session):
    """Ordered list of active follow-up templates, by step_order."""
    templates = session.exec(
        select(Template).where(Template.kind == "followup", Template.active == True)
    ).all()
    return sorted(templates, key=lambda t: t.step_order or 0)


def _first_touch_templates(session):
    return session.exec(
        select(Template).where(Template.kind == "first_touch", Template.active == True)
    ).all()


# ---------- Commands ----------

def cmd_check(days=1):
    init_db()
    session = get_session()
    mailboxes = _active_mailboxes(session)
    if not mailboxes:
        print("No verified/active mailboxes configured.")
        return

    since_date = (utcnow() - timedelta(days=days)).strftime("%d-%b-%Y")
    all_replied_from = set()
    for mb in mailboxes:
        print(f"Scanning {mb.email} since {since_date} ...")
        try:
            password = decrypt_password(mb.encrypted_password)
            found = mailer.scan_inbox_for_replies(mb.email, password, since_date)
            all_replied_from |= found
        except Exception as e:
            print(f"  Could not scan {mb.email}: {e}")

    matched = 0
    for sender_email in all_replied_from:
        latest = session.exec(
            select(SendLog).where(SendLog.contact_email == sender_email)
            .order_by(SendLog.follow_up_number.desc())
        ).first()
        if latest and not latest.replied:
            latest.replied = True
            latest.reply_date = utcnow()
            session.add(latest)
            matched += 1
            print(f"  -> Reply detected from {sender_email}")

    session.commit()
    session.close()
    print(f"Done. {matched} new repl{'y' if matched == 1 else 'ies'} marked.")


def cmd_followup(limit=None, dry_run=False):
    init_db()
    session = get_session()
    mailboxes = _active_mailboxes(session)
    if not mailboxes:
        print("No verified/active mailboxes configured.")
        return

    followup_templates = _followup_templates(session)
    if not followup_templates:
        print("No active follow-up templates configured — nothing to send.")
        return

    sent_today = _sent_today_count(session)
    cap = limit or DAILY_SEND_CAP
    remaining = max(cap - sent_today, 0)
    if remaining == 0:
        print(f"Daily cap ({cap}) already reached today. Stopping.")
        return

    all_contacts = session.exec(select(Contact)).all()
    print(f"Checking {len(all_contacts)} contacts for due follow-ups...")
    if dry_run:
        print("DRY RUN — no emails will actually be sent.\n")

    sent_count = 0
    for contact in all_contacts:
        if sent_count >= remaining:
            print(f"Reached remaining budget ({remaining}). Stopping.")
            break

        latest = session.exec(
            select(SendLog).where(SendLog.contact_id == contact.id)
            .order_by(SendLog.follow_up_number.desc())
        ).first()
        if not latest or latest.replied or latest.bounced:
            continue  # never contacted yet, already replied, or a dead address — skip

        step_index = latest.follow_up_number - 1
        if step_index >= len(followup_templates):
            continue  # sequence exhausted

        gap_required = followup_templates[step_index].gap_days or 3
        days_elapsed = (utcnow() - latest.date_sent).days
        if days_elapsed < gap_required:
            continue

        template = followup_templates[step_index]
        mailbox = _mailbox_for_contact(mailboxes, contact.id)

        fields = _build_merge_fields(contact)
        body = _render(template.body, fields)
        first_name = mailbox.from_name
        body = f"{body}\n\nBest,\n{first_name}"

        first_log = session.exec(
            select(SendLog).where(SendLog.contact_id == contact.id, SendLog.follow_up_number == 1)
        ).first()
        base_subject = first_log.subject if first_log else contact.business_name
        subject = base_subject if base_subject.lower().startswith("re:") else f"Re: {base_subject}"

        new_references = (f"{latest.references} " if latest.references else "") + (latest.message_id or "")
        new_fup = latest.follow_up_number + 1



        if dry_run:
            print(f"  [DRY RUN] Would send '{template.name}' to {contact.business_name} "
                  f"<{contact.contact_email}> (msg #{new_fup}, {days_elapsed}d since last, threaded)")
        else:
            try:
                password = decrypt_password(mailbox.encrypted_password)
                msg_id = mailer.send_email(
                    mailbox.email, password, mailbox.from_name, contact.contact_email,
                    subject, body, in_reply_to=latest.message_id, references=new_references,
                )
                print(f"  Sent '{template.name}' to {contact.business_name} <{contact.contact_email}> (msg #{new_fup})")
            except mailer.PermanentBounce as e:
                print(f"  BOUNCED (permanent — will never retry) {contact.contact_email}: {e}")
                log = SendLog(
                    contact_id=contact.id, contact_email=contact.contact_email,
                    business_name=contact.business_name, template_id=template.id, template_name=template.name,
                    mailbox_id=mailbox.id, mailbox_email=mailbox.email, follow_up_number=new_fup,
                    subject=subject, bounced=True, notes=str(e)[:500],
                )
                session.add(log)
                session.commit()
                sent_count += 1
                continue
            except mailer.TransientSendError as e:
                print(f"  FAILED (temporary — will retry next run) to send to {contact.contact_email}: {e}")
                continue

            log = SendLog(
                contact_id=contact.id, contact_email=contact.contact_email,
                business_name=contact.business_name, template_id=template.id, template_name=template.name,
                mailbox_id=mailbox.id, mailbox_email=mailbox.email, follow_up_number=new_fup,
                subject=subject, message_id=msg_id,
                in_reply_to=latest.message_id, references=new_references,
            )
            session.add(log)
            session.commit()

        sent_count += 1
        if not dry_run and sent_count < remaining:
            mailer.random_delay()

    session.close()
    print(f"\nDone. {sent_count} follow-up(s) {'previewed' if dry_run else 'sent and logged'}.")


def cmd_send(limit=None, dry_run=False):
    init_db()
    session = get_session()
    mailboxes = _active_mailboxes(session)
    if not mailboxes:
        print("No verified/active mailboxes configured.")
        return

    first_touch_templates = _first_touch_templates(session)
    if not first_touch_templates:
        print("No active first-touch templates configured — nothing to send.")
        return

    sent_today = _sent_today_count(session)
    cap = limit or DAILY_SEND_CAP
    remaining = max(cap - sent_today, 0)
    if remaining == 0:
        print(f"Daily cap ({cap}) already reached today. Stopping.")
        return

    already_contacted_ids = {row for row in session.exec(select(SendLog.contact_id).distinct())}
    all_contacts = session.exec(select(Contact)).all()
    pending = [c for c in all_contacts if c.id not in already_contacted_ids]

    print(f"Daily cap: {cap} | Sent today: {sent_today} | Remaining: {remaining} | Pending new contacts: {len(pending)}")
    if dry_run:
        print("DRY RUN — no emails will actually be sent.\n")

    sent_count = 0
    for i, contact in enumerate(pending):
        if sent_count >= remaining:
            print(f"Reached remaining budget ({remaining}). Stopping.")
            break

        # Auto-rotate: if the contact wasn't assigned a specific template,
        # round-robin across active first-touch templates for even test groups.
        if contact.first_touch_template_id:
            template = session.get(Template, contact.first_touch_template_id)
        else:
            template = first_touch_templates[i % len(first_touch_templates)]

        if not template:
            print(f"  SKIP {contact.contact_email}: no valid template resolved")
            continue

        fields = _build_merge_fields(contact)
        mailbox = _mailbox_for_contact(mailboxes, contact.id)

        subject = _render(template.subject, fields)
        body = _render(template.body, fields)

        # Extract the first name from the mailbox and append the signature
        first_name = mailbox.from_name
        body = f"{body}\n\nBest,\n{first_name}"


        if dry_run:
            print(f"  [DRY RUN] Would send '{template.name}' to {contact.business_name} "
                  f"<{contact.contact_email}> via {mailbox.email}")
        else:
            try:
                password = decrypt_password(mailbox.encrypted_password)
                msg_id = mailer.send_email(
                    mailbox.email, password, mailbox.from_name, contact.contact_email, subject, body,
                )
                print(f"  Sent '{template.name}' to {contact.business_name} <{contact.contact_email}> via {mailbox.email}")
            except mailer.PermanentBounce as e:
                print(f"  BOUNCED (permanent — will never retry) {contact.contact_email}: {e}")
                log = SendLog(
                    contact_id=contact.id, contact_email=contact.contact_email,
                    business_name=contact.business_name, template_id=template.id, template_name=template.name,
                    mailbox_id=mailbox.id, mailbox_email=mailbox.email, follow_up_number=1,
                    subject=subject, bounced=True, notes=str(e)[:500],
                )
                session.add(log)
                session.commit()
                sent_count += 1  # counts against budget — it did consume a send attempt
                continue
            except mailer.TransientSendError as e:
                print(f"  FAILED (temporary — will retry next run) to send to {contact.contact_email}: {e}")
                continue  # not logged — stays in the "pending" pool for next time

            log = SendLog(
                contact_id=contact.id, contact_email=contact.contact_email,
                business_name=contact.business_name, template_id=template.id, template_name=template.name,
                mailbox_id=mailbox.id, mailbox_email=mailbox.email, follow_up_number=1,
                subject=subject, message_id=msg_id,
            )
            session.add(log)
            session.commit()

        sent_count += 1
        if not dry_run and sent_count < remaining:
            mailer.random_delay()

    session.close()
    print(f"\nDone. {sent_count} email(s) {'previewed' if dry_run else 'sent and logged'}.")


def cmd_pull_leads():
    if not os.getenv("GOOGLE_SHEET_ID"):
        print("GOOGLE_SHEET_ID not set — skipping Google Sheets pull (optional feature).")
        return
    try:
        added = sheets_sync.pull_leads()
        print(f"Pulled {added} new lead(s) from Google Sheet.")
    except Exception as e:
        print(f"Google Sheets pull failed (continuing anyway): {e}")


def cmd_push_status():
    if not os.getenv("GOOGLE_SHEET_ID"):
        return
    try:
        updated = sheets_sync.push_status()
        print(f"Updated status for {updated} row(s) in Google Sheet.")
    except Exception as e:
        print(f"Google Sheets status push failed (continuing anyway): {e}")


def cmd_run_daily(limit=None, check_days=1, dry_run=False):
    """Runs the full daily cycle in the safe order:
    0. Pull any new leads from the Google Sheet (if configured)
    1. Check replies FIRST (so anyone who just replied is marked before we
       decide who to email)
    2. Send due follow-ups (warm contacts get priority for the daily budget)
    3. Send new first-touch (fills whatever budget remains)
    4. Push updated status back to the Sheet (if configured)"""
    print("=== STEP 0/4: Pulling new leads from Google Sheet (if configured) ===")
    cmd_pull_leads()

    print("\n=== STEP 1/4: Checking for replies (before sending anything) ===")
    cmd_check(days=check_days)

    print("\n=== STEP 2/4: Sending due follow-ups ===")
    cmd_followup(limit=limit, dry_run=dry_run)

    print("\n=== STEP 3/4: Sending new first-touch batch ===")
    cmd_send(limit=limit, dry_run=dry_run)

    print("\n=== STEP 4/4: Pushing status back to Google Sheet (if configured) ===")
    cmd_push_status()


def main():
    parser = argparse.ArgumentParser(description="Outreach sending engine")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check")
    p_check.add_argument("--days", type=int, default=1)

    p_followup = sub.add_parser("followup")
    p_followup.add_argument("--limit", type=int, default=None)
    p_followup.add_argument("--dry-run", action="store_true")

    p_send = sub.add_parser("send")
    p_send.add_argument("--limit", type=int, default=None)
    p_send.add_argument("--dry-run", action="store_true")

    p_daily = sub.add_parser("run-daily")
    p_daily.add_argument("--limit", type=int, default=None)
    p_daily.add_argument("--check-days", type=int, default=1)
    p_daily.add_argument("--dry-run", action="store_true")

    p_pull = sub.add_parser("pull-leads", help="Pull new leads from Google Sheet only")

    args = parser.parse_args()

    if args.command == "check":
        cmd_check(days=args.days)
    elif args.command == "followup":
        cmd_followup(limit=args.limit, dry_run=args.dry_run)
    elif args.command == "send":
        cmd_send(limit=args.limit, dry_run=args.dry_run)
    elif args.command == "run-daily":
        cmd_run_daily(limit=args.limit, check_days=args.check_days, dry_run=args.dry_run)
    elif args.command == "pull-leads":
        cmd_pull_leads()


if __name__ == "__main__":
    main()