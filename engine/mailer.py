import os
import time
import random
import smtplib
import imaplib
import email as email_lib
from email.mime.text import MIMEText
from email.header import decode_header
from email.utils import formatdate, make_msgid

SMTP_HOST = os.getenv("SPACEMAIL_SMTP_HOST", "mail.spacemail.com")
SMTP_PORT = int(os.getenv("SPACEMAIL_SMTP_PORT", "465"))
IMAP_HOST = os.getenv("SPACEMAIL_IMAP_HOST", "mail.spacemail.com")
IMAP_PORT = int(os.getenv("SPACEMAIL_IMAP_PORT", "993"))
SENT_FOLDER = os.getenv("SENT_FOLDER", "Sent")

MIN_DELAY_SECONDS = int(os.getenv("MIN_DELAY_SECONDS", "20"))
MAX_DELAY_SECONDS = int(os.getenv("MAX_DELAY_SECONDS", "50"))


class PermanentBounce(Exception):
    """The recipient address was definitively rejected (invalid mailbox,
    domain doesn't exist, etc). Never retry — the address is bad."""


class TransientSendError(Exception):
    """A temporary failure (connection issue, timeout, brief server problem).
    Safe to retry on the next run — not the recipient's fault."""


def verify_mailbox(email_address: str, password: str) -> tuple[bool, str]:
    """Verifies via SMTP_SSL login — matches the exact connection method
    already confirmed working. IMAP is checked too (needed for reply-scanning
    and filing Sent-folder copies), but a mailbox is still usable for sending
    even if IMAP has an issue, so IMAP failure is reported, not blocking."""
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.login(email_address, password)
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP authentication failed — username or password incorrect"
    except Exception as e:
        return False, f"SMTP connection error: {e}"

    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(email_address, password)
        imap.logout()
    except Exception as e:
        return True, f"SMTP login OK, but IMAP check failed ({e}) — sending will work, but reply-checking and Sent-folder copies may not."

    return True, "SMTP and IMAP login both successful"


def send_email(email_address, password, from_name, to_email, subject, body,
                in_reply_to=None, references=None):
    """Sends via SMTP_SSL, threads if in_reply_to is given, files a
    Sent-folder copy via IMAP APPEND. Returns the Message-ID.

    Raises PermanentBounce if the recipient was definitively rejected
    (invalid mailbox/domain — don't retry), or TransientSendError for
    anything else (connection issues, timeouts — safe to retry later)."""
    domain = email_address.split("@")[-1]
    msg_id = make_msgid(domain=domain)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{email_address}>" if from_name else email_address
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = msg_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    raw_message = msg.as_bytes()

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.login(email_address, password)
            server.sendmail(email_address, [to_email], raw_message)
    except smtplib.SMTPRecipientsRefused as e:
        # Server rejected the recipient address outright — this means the
        # address is invalid/doesn't exist. Permanent, don't retry.
        raise PermanentBounce(f"Recipient refused: {e}")
    except smtplib.SMTPResponseException as e:
        # Any other SMTP-level error with a status code. 5xx = permanent,
        # 4xx = temporary (server busy, greylisting, etc).
        if e.smtp_code >= 500:
            raise PermanentBounce(f"SMTP {e.smtp_code}: {e.smtp_error}")
        raise TransientSendError(f"SMTP {e.smtp_code}: {e.smtp_error}")
    except Exception as e:
        # Connection errors, timeouts, auth issues — none of these are the
        # recipient's fault, so treat as retryable.
        raise TransientSendError(str(e))

    _append_to_sent(email_address, password, raw_message)
    return msg_id


def _append_to_sent(email_address, password, raw_message):
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(email_address, password)
        imap.append(SENT_FOLDER, "\\Seen", imaplib.Time2Internaldate(time.time()), raw_message)
        imap.logout()
    except Exception as e:
        print(f"    (note: sent, but couldn't file a copy in '{SENT_FOLDER}' for {email_address}: {e})")


def _decode_mime_words(s):
    if not s:
        return ""
    parts = decode_header(s)
    decoded = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            decoded += text.decode(enc or "utf-8", errors="ignore")
        else:
            decoded += text
    return decoded


def scan_inbox_for_replies(email_address, password, since_date_str):
    found = set()
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap.login(email_address, password)
    imap.select("INBOX")

    status, data = imap.search(None, f'(SINCE "{since_date_str}")')
    if status != "OK":
        imap.logout()
        return found

    for msg_id in data[0].split():
        status, msg_data = imap.fetch(msg_id, "(RFC822)")
        if status != "OK":
            continue
        raw = msg_data[0][1]
        msg = email_lib.message_from_bytes(raw)
        from_header = _decode_mime_words(msg.get("From", ""))
        from_email = from_header.split("<")[-1].replace(">", "").strip().lower()
        if from_email:
            found.add(from_email)

    imap.logout()
    return found


def random_delay():
    time.sleep(random.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))