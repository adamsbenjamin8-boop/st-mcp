"""
Email Monitor — watches orders@denommeeplumbing.com for new quote emails
and saves attachments to the Incoming Quotes folder for processing.

Uses IMAP over SSL — no Azure app registration or Power Automate needed.
Runs as a background thread inside the desktop app.

Supported attachment types: .pdf, .csv, .xlsx
"""

import email
import imaplib
import os
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import QUOTES_INBOX_FOLDER, ORDERS_EMAIL
from approved_senders import (
    is_approved, get_domain, queue_for_approval,
    process_approval_email, sync_from_smartsheet
)

# ---------------------------------------------------------------------------
# IMAP settings for Microsoft 365
# ---------------------------------------------------------------------------
IMAP_HOST = "outlook.office365.com"
IMAP_PORT = 993
IMAP_FOLDER = "INBOX"
POLL_SECONDS = 120   # Check every 2 minutes

SUPPORTED_EXTENSIONS = {'.pdf', '.csv', '.xlsx'}


class EmailMonitor:
    """
    Polls the orders mailbox for new emails with quote attachments.
    Saves attachments to the Incoming Quotes folder.
    """

    def __init__(self, email_address: str, password: str, dest_folder: Path):
        self.email_address = email_address
        self.password      = password
        self.dest_folder   = dest_folder
        self.last_checked: Optional[datetime] = None
        self.last_status   = "Not yet checked"
        self.is_running    = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the background polling thread."""
        if not self.email_address or not self.password:
            self.last_status = "Email credentials not configured"
            return
        self.dest_folder.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def check_now(self):
        """Trigger an immediate check (called from UI button)."""
        threading.Thread(target=self._do_check, daemon=True).start()

    def _loop(self):
        # Sync approved senders on first start
        sync_from_smartsheet()
        while True:
            self._do_check()
            time.sleep(POLL_SECONDS)

    def _do_check(self):
        if self.is_running:
            return
        self.is_running = True
        try:
            new_files = self._fetch_attachments()
            self.last_checked = datetime.now()
            if new_files:
                self.last_status = (
                    f"Last check: {self.last_checked.strftime('%b %d %I:%M %p')} "
                    f"— {len(new_files)} file{'s' if len(new_files) != 1 else ''} saved"
                )
            else:
                self.last_status = f"Last check: {self.last_checked.strftime('%b %d %I:%M %p')} — no new quotes"
        except Exception as e:
            self.last_status = f"Email check failed: {e}"
        finally:
            self.is_running = False

    def _fetch_attachments(self) -> list:
        """
        Connect to IMAP, find unread emails with attachments, save them.
        Returns list of saved file paths.
        """
        saved = []

        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as mail:
            mail.login(self.email_address, self.password)
            mail.select(IMAP_FOLDER)

            # Search for unread emails
            _, msg_ids = mail.search(None, "UNSEEN")
            if not msg_ids or not msg_ids[0]:
                return saved

            for msg_id in msg_ids[0].split():
                try:
                    _, msg_data = mail.fetch(msg_id, "(RFC822)")
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw)

                    sender  = msg.get("From", "")
                    subject = msg.get("Subject", "")
                    date    = msg.get("Date", "")

                    # Check for vendor approval confirmation first
                    if process_approval_email(subject):
                        mail.store(msg_id, "+FLAGS", "\\Seen")
                        continue

                    # Enforce sender allowlist
                    sender_domain = get_domain(sender)
                    if not is_approved(sender_domain):
                        print(f"  🔒 Unknown sender: {sender} — queuing for approval")
                        # Extract vendor name from sender display name if possible
                        vendor_name = sender.split("<")[0].strip().strip('"') or sender_domain
                        # Look up ST vendor ID
                        st_vendor_id = ""
                        try:
                            from st_client import find_vendor_id
                            vid = find_vendor_id(vendor_name)
                            if vid:
                                st_vendor_id = str(vid)
                        except Exception:
                            pass
                        queue_for_approval(
                            vendor_name=vendor_name,
                            email_domain=sender_domain,
                            st_vendor_id=st_vendor_id,
                            sender_email=sender,
                            notes=f"Subject: {subject}",
                        )
                        # Move to Quarantine instead of processing
                        quarantine = self.dest_folder.parent / "Quarantine"
                        quarantine.mkdir(parents=True, exist_ok=True)
                        # Save attachment to Quarantine
                        for part in msg.walk():
                            if part.get("Content-Disposition") is None:
                                continue
                            filename = part.get_filename()
                            if filename:
                                data = part.get_payload(decode=True)
                                if data:
                                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                                    (quarantine / f"{ts}_{filename}").write_bytes(data)
                        mail.store(msg_id, "+FLAGS", "\\Seen")
                        continue

                    # Walk attachments (approved sender)
                    for part in msg.walk():
                        if part.get_content_maintype() == "multipart":
                            continue
                        if part.get("Content-Disposition") is None:
                            continue

                        filename = part.get_filename()
                        if not filename:
                            continue

                        # Decode if needed
                        if isinstance(filename, bytes):
                            filename = filename.decode("utf-8", errors="replace")

                        # Only process supported file types
                        ext = Path(filename).suffix.lower()
                        if ext not in SUPPORTED_EXTENSIONS:
                            continue

                        # Save to inbox folder with timestamp to avoid collisions
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        safe_name = "".join(c for c in Path(filename).stem if c.isalnum() or c in "._- ")
                        dest_filename = f"{ts}_{safe_name}{ext}"
                        dest_path = self.dest_folder / dest_filename

                        data = part.get_payload(decode=True)
                        if data:
                            dest_path.write_bytes(data)
                            saved.append(str(dest_path))
                            print(f"  📎 Saved attachment: {dest_filename}")
                            print(f"     From: {sender} | Subject: {subject}")

                    # Mark as read after processing
                    if saved:
                        mail.store(msg_id, "+FLAGS", "\\Seen")

                except Exception as e:
                    print(f"  Error processing message {msg_id}: {e}")

        return saved


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Factory — reads credentials from environment/.env
# ---------------------------------------------------------------------------
def create_monitor() -> EmailMonitor:
    """Create an EmailMonitor using credentials from environment."""
    email_addr = os.environ.get("ORDERS_EMAIL_ADDRESS", ORDERS_EMAIL)
    password   = os.environ.get("ORDERS_EMAIL_PASSWORD", "")
    return EmailMonitor(
        email_address=email_addr,
        password=password,
        dest_folder=QUOTES_INBOX_FOLDER,
    )


if __name__ == "__main__":
    """Run as a standalone background process."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))

    # Load .env
    env_paths = [
        Path(__file__).parent.parent / ".env",
        Path("C:/Program Files/ST_MCP/.env"),
    ]
    for ep in env_paths:
        if ep.exists():
            for line in ep.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
            break

    monitor = create_monitor()
    if not monitor.password:
        print("ORDERS_EMAIL_PASSWORD not set — email monitor not starting")
        sys.exit(0)

    print(f"Email monitor starting — watching {monitor.email_address}")
    print(f"Saving attachments to: {monitor.dest_folder}")
    monitor.start()

    # Keep alive
    while True:
        time.sleep(60)


