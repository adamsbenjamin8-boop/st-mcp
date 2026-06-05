"""
Email Monitor — watches orders@denommeeplumbing.com via Microsoft Graph API (OAuth2).
Saves attachments + metadata sidecar for quote processing.
Also handles [VENDOR_MAP] emails from Smartsheet automation.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
import json
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional
import httpx

from config import QUOTES_INBOX_FOLDER, ORDERS_EMAIL

# ---------------------------------------------------------------------------
# Microsoft Graph settings
# ---------------------------------------------------------------------------
GRAPH_AUTH_URL   = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_BASE       = "https://graph.microsoft.com/v1.0"
POLL_SECONDS     = 120
SUPPORTED_EXTS   = {'.pdf', '.csv', '.xlsx'}

# Local file to track processed message IDs (prevents reprocessing if mark-as-read fails)
PROCESSED_IDS_FILE = Path("C:/Program Files/ST_MCP/processed_email_ids.json")

def _load_processed_ids() -> set:
    try:
        if PROCESSED_IDS_FILE.exists():
            data = __import__('json').loads(PROCESSED_IDS_FILE.read_text(encoding='utf-8'))
            return set(data.get("ids", []))
    except Exception:
        pass
    return set()

def _save_processed_id(msg_id: str):
    try:
        ids = _load_processed_ids()
        ids.add(msg_id)
        ids_list = list(ids)[-500:]
        PROCESSED_IDS_FILE.write_text(
            __import__('json').dumps({"ids": ids_list}, indent=2), encoding='utf-8'
        )
    except Exception as e:
        print(f"  WARNING: Could not save processed ID: {e}")


# ---------------------------------------------------------------------------
# Load credentials
# ---------------------------------------------------------------------------
def _load_env():
    for env_file in [
        Path(__file__).parent.parent / ".env",
        Path("C:/Program Files/ST_MCP/.env"),
    ]:
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    os.environ[k.strip()] = v.strip()
            break

_load_env()

_graph_token_cache = {"token": None, "expires_at": 0.0}


def _get_graph_token() -> Optional[str]:
    now = time.monotonic()
    if _graph_token_cache["token"] and now < _graph_token_cache["expires_at"] - 30:
        return _graph_token_cache["token"]

    tenant_id     = os.environ.get("AZURE_TENANT_ID", "")
    client_id     = os.environ.get("AZURE_CLIENT_ID", "")
    client_secret = os.environ.get("AZURE_CLIENT_SECRET", "")

    if not all([tenant_id, client_id, client_secret]):
        return None

    try:
        resp = httpx.post(
            GRAPH_AUTH_URL.format(tenant=tenant_id),
            data={
                "grant_type":    "client_credentials",
                "client_id":     client_id,
                "client_secret": client_secret,
                "scope":         "https://graph.microsoft.com/.default",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        _graph_token_cache["token"]      = data["access_token"]
        _graph_token_cache["expires_at"] = now + data.get("expires_in", 3600)
        return _graph_token_cache["token"]
    except Exception as e:
        print(f"Graph auth failed: {e}")
        return None


def _graph_headers() -> dict:
    token = _get_graph_token()
    if not token:
        raise RuntimeError("No Graph token available")
    return {"Authorization": f"Bearer {token}"}


class EmailMonitor:
    def __init__(self, mailbox: str, dest_folder: Path):
        self.mailbox     = mailbox
        self.dest_folder = dest_folder
        self.last_checked: Optional[datetime] = None
        self.last_status = "Not yet checked"
        self.is_running  = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        token = _get_graph_token()
        if not token:
            self.last_status = "Azure credentials not configured — email monitor not starting"
            print(f"  WARNING: {self.last_status}")
            return
        self.dest_folder.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def check_now(self):
        threading.Thread(target=self._do_check, daemon=True).start()

    def _loop(self):
        while True:
            self._do_check()
            time.sleep(POLL_SECONDS)

    def _do_check(self):
        if self.is_running:
            return
        self.is_running = True
        try:
            saved = self._fetch_attachments()
            self.last_checked = datetime.now()
            if saved:
                self.last_status = (
                    f"Last check: {self.last_checked.strftime('%b %d %I:%M %p')} "
                    f"- {len(saved)} file{'s' if len(saved) != 1 else ''} saved"
                )
            else:
                self.last_status = f"Last check: {self.last_checked.strftime('%b %d %I:%M %p')} - no new quotes"
        except Exception as e:
            self.last_status = f"Email check failed: {e}"
            print(f"  ERROR: Email check error: {e}")
        finally:
            self.is_running = False

    def _fetch_attachments(self) -> list:
        saved    = []
        msgs_url = (
            f"{GRAPH_BASE}/users/{self.mailbox}/mailFolders/inbox/messages"
            f"?$filter=isRead eq false&$top=20"
            f"&$select=id,subject,from,receivedDateTime,body"
        )
        try:
            r = httpx.get(msgs_url, headers=_graph_headers(), timeout=30)
            r.raise_for_status()
            messages = r.json().get("value", [])
        except Exception as e:
            print(f"  ERROR: Could not fetch messages: {e}")
            return saved

        processed_ids = _load_processed_ids()
        for msg in messages:
            msg_id  = msg.get("id", "")
            subject = msg.get("subject", "")
            sender  = msg.get("from", {}).get("emailAddress", {}).get("address", "")
            body    = msg.get("body", {}).get("content", "")[:2000]

            # Skip already-processed messages (prevents reprocessing if mark-as-read fails)
            if msg_id in processed_ids:
                self._mark_read(msg_id)
                continue

            # Handle vendor mapping emails from Smartsheet (always process regardless of sender)
            if "[VENDOR_MAP]" in subject:
                self._handle_vendor_map_email(subject)
                _save_processed_id(msg_id)
                self._mark_read(msg_id)
                continue

           

            # Fetch attachments
            att_url = f"{GRAPH_BASE}/users/{self.mailbox}/messages/{msg_id}/attachments"
            try:
                r2 = httpx.get(att_url, headers=_graph_headers(), timeout=30)
                r2.raise_for_status()
                attachments = r2.json().get("value", [])
            except Exception:
                continue

            found_attachment = False
            for att in attachments:
                filename = att.get("name", "")
                ext      = Path(filename).suffix.lower()
                if ext not in SUPPORTED_EXTS:
                    continue

                content_bytes = att.get("contentBytes")
                if not content_bytes:
                    continue

                import base64
                data = base64.b64decode(content_bytes)

                ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_name = "".join(c for c in Path(filename).stem if c.isalnum() or c in "._- ")
                dest_name = f"{ts}_{safe_name}{ext}"
                dest_path = self.dest_folder / dest_name

                dest_path.write_bytes(data)
                saved.append(str(dest_path))
                found_attachment = True

                # Save metadata sidecar
                meta = {
                    "subject": subject,
                    "body":    body,
                    "sender":  sender,
                    "date":    msg.get("receivedDateTime", ""),
                }
                dest_path.with_suffix(".meta.json").write_text(
                    json.dumps(meta, ensure_ascii=False), encoding="utf-8"
                )
                print(f"  Saved: {dest_name} | From: {sender}")

            if found_attachment:
                _save_processed_id(msg_id)
                self._mark_read(msg_id)
            elif not attachments:
                _save_processed_id(msg_id)
                self._mark_read(msg_id)

        return saved

    def _mark_read(self, msg_id: str):
        try:
            httpx.patch(
                f"{GRAPH_BASE}/users/{self.mailbox}/messages/{msg_id}",
                headers={**_graph_headers(), "Content-Type": "application/json"},
                json={"isRead": True},
                timeout=15,
            )
        except Exception:
            pass

    def _handle_vendor_map_email(self, subject: str):
        try:
            body = subject.replace("[VENDOR_MAP]", "").strip()
            if "=" not in body:
                return
            quote_name, _, st_name = body.partition("=")
            quote_name = quote_name.strip()
            st_name    = st_name.strip()
            if quote_name and st_name:
                from st_client import save_vendor_mapping
                save_vendor_mapping(quote_name, st_name)
                print(f"  Vendor mapping updated: '{quote_name}' -> '{st_name}'")
        except Exception as e:
            print(f"  WARNING: Could not process vendor map email: {e}")


def create_monitor() -> EmailMonitor:
    mailbox = os.environ.get("ORDERS_EMAIL_ADDRESS", ORDERS_EMAIL)
    return EmailMonitor(mailbox=mailbox, dest_folder=QUOTES_INBOX_FOLDER)


if __name__ == "__main__":
    monitor = create_monitor()
    token   = _get_graph_token()
    if not token:
        print("Azure credentials not configured — email monitor not starting")
        print("Required: AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET in .env")
        sys.exit(0)
    print(f"Email monitor starting — watching {monitor.mailbox}")
    print(f"Saving attachments to: {monitor.dest_folder}")
    monitor.start()
    while True:
        time.sleep(60)
