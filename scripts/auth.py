"""Shared OAuth credentials for Gmail, Drive, and Sheets.

Uses a single user-consented OAuth client rather than a service account:
a consumer @gmail.com inbox cannot be read by a service account at all,
and acting as the user removes the need to share folders/sheets with a
separate robot address.

Run scripts/authorize.py once to perform the browser consent.
"""
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

CREDENTIALS_DIR = Path(__file__).resolve().parent.parent / "credentials"
CLIENT_SECRETS_PATH = CREDENTIALS_DIR / "oauth-client.json"
TOKEN_PATH = CREDENTIALS_DIR / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    # Full (not .readonly/.file) Drive scope: this agent both reads the
    # watched Invoice_Automation folder tree AND creates/writes its own
    # "Agent Data" subfolder + sheets inside a folder it didn't create —
    # drive.file's "only files this app created or the user opened with
    # it" restriction doesn't cover that pre-existing parent folder.
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_credentials(interactive: bool = False) -> Credentials:
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            return creds
        except RefreshError:
            # Refresh token itself was revoked/expired (e.g. unused >6 months,
            # or consent revoked) -- fall through to a fresh interactive
            # consent instead of crashing, same as if no token existed at all.
            creds = None

    if not interactive:
        raise RuntimeError(
            f"No usable Google credentials at {TOKEN_PATH}. "
            "Run: python scripts/authorize.py"
        )

    if not CLIENT_SECRETS_PATH.exists():
        raise RuntimeError(f"Missing OAuth client file at {CLIENT_SECRETS_PATH}.")

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())
    return creds
