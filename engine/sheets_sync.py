"""
One-way-per-field Google Sheets sync — no conflicts possible, because each
column has exactly one owner:

  INPUT columns (you type these):  business_name, contact_email,
      first_touch_template (optional), plus any extra columns you add
      (become {{field}} personalization placeholders automatically)

  OUTPUT columns (the system writes these, you never touch them):
      message_no_sent, replied

Required header names in row 1 of your sheet (exact, case-sensitive):
    business_name | contact_email | message_no_sent | replied
Any other columns are yours to add freely.

Setup:
  1. Create a Google Cloud project, enable the Google Sheets API.
  2. Create a Service Account, download its JSON key.
  3. Share your Google Sheet with the service account's email address
     (found inside the JSON key file), giving it Editor access.
  4. Set GOOGLE_SERVICE_ACCOUNT_JSON (the full JSON key, as one string) and
     GOOGLE_SHEET_ID (from the sheet's URL) as env vars / secrets.
"""

import os
import json

import gspread
from google.oauth2.service_account import Credentials
from sqlmodel import select

from shared.db import get_session
from shared.models import Contact, SendLog, Template

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

RESERVED_INPUT_COLUMNS = {"business_name", "contact_email", "first_touch_template"}
OUTPUT_COLUMNS = {"message_no_sent", "replied"}

GREEN = {"red": 0.72, "green": 0.88, "blue": 0.80}   # replied
YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.60}   # sent, not replied yet


def _get_worksheet(sheet_id: str, tab_name: str):
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not creds_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set.")
    if not sheet_id:
        raise RuntimeError("No Google Sheet ID provided.")

    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    client = gspread.authorize(creds)
    sh = client.open_by_key(sheet_id)
    return sh.worksheet(tab_name)


def pull_leads(campaign) -> int:
    """Reads every row from the sheet; any contact_email not already in the
    DB becomes a new Contact. Safe to run repeatedly — existing contacts
    (matched by email) are always skipped, never duplicated or overwritten."""

    sheet_id = campaign.google_sheet_id or os.getenv("GOOGLE_SHEET_ID")
    tab_name = campaign.google_sheet_tab or "Sheet1"

    ws = _get_worksheet(sheet_id, tab_name)
    records = ws.get_all_records()  # list of dicts, keyed by header row

    session = get_session()
    # Filter existing emails only for this campaign? Or globally?
    # Usually globally is fine to prevent spamming same lead from multiple campaigns,
    # or by campaign if you want to allow it. Let's do by campaign to be safer or global.
    # The current code does it globally. Let's keep it global.
    existing_emails = {c.contact_email for c in session.exec(select(Contact)).all()}

    # We should only use templates for THIS campaign, but old code used all templates.
    # Let's get templates for this campaign.
    templates_by_name = {t.name: t for t in session.exec(select(Template).where(Template.campaign_id == campaign.id)).all()}

    added = 0
    for row in records:
        email = str(row.get("contact_email", "")).strip().lower()
        business_name = str(row.get("business_name", "")).strip()
        if not email or not business_name or email in existing_emails:
            continue

        template_id = None
        template_name = str(row.get("first_touch_template", "")).strip()
        if template_name and template_name in templates_by_name:
            template_id = templates_by_name[template_name].id

        extra_info = {
            k: v for k, v in row.items()
            if k not in RESERVED_INPUT_COLUMNS and k not in OUTPUT_COLUMNS and str(v).strip()
        }

        contact = Contact(
            business_name=business_name, contact_email=email,
            campaign_id=campaign.id,
            first_touch_template_id=template_id, extra_info=extra_info,
        )
        session.add(contact)
        existing_emails.add(email)
        added += 1

    session.commit()
    session.close()
    return added


def push_status(campaign) -> int:
    """Writes message_no_sent + replied back to matching rows (by email),
    and colors the replied cell green (replied) or yellow (sent, waiting)."""
    sheet_id = campaign.google_sheet_id or os.getenv("GOOGLE_SHEET_ID")
    tab_name = campaign.google_sheet_tab or "Sheet1"

    ws = _get_worksheet(sheet_id, tab_name)
    all_values = ws.get_all_values()
    if not all_values:
        return 0

    headers = all_values[0]
    try:
        email_col = headers.index("contact_email")
        msg_col = headers.index("message_no_sent")
        replied_col = headers.index("replied")
    except ValueError:
        raise RuntimeError(
            "Sheet must have 'contact_email', 'message_no_sent', and 'replied' "
            "column headers (exact names, row 1)."
        )

    session = get_session()
    value_updates = []
    format_requests = []
    updated = 0

    for row_idx, row in enumerate(all_values[1:], start=2):  # 1-indexed, header is row 1
        if email_col >= len(row):
            continue
        email = row[email_col].strip().lower()
        if not email:
            continue

        latest = session.exec(
            select(SendLog).where(SendLog.contact_email == email)
            .order_by(SendLog.follow_up_number.desc())
        ).first()
        if not latest:
            continue  # never contacted yet — leave blank, no color

        value_updates.append({"range": gspread.utils.rowcol_to_a1(row_idx, msg_col + 1),
                               "values": [[latest.follow_up_number]]})
        value_updates.append({"range": gspread.utils.rowcol_to_a1(row_idx, replied_col + 1),
                               "values": [["TRUE" if latest.replied else "FALSE"]]})

        color = GREEN if latest.replied else YELLOW
        format_requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": row_idx - 1, "endRowIndex": row_idx,
                    "startColumnIndex": replied_col, "endColumnIndex": replied_col + 1,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })
        updated += 1

    if value_updates:
        ws.batch_update(value_updates, value_input_option="RAW")
    if format_requests:
        ws.spreadsheet.batch_update({"requests": format_requests})

    session.close()
    return updated
