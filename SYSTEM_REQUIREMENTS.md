# System requirements

What a new client needs, on a brand-new computer, to set up and run the invoice agent. For the step-by-step install, see `README.md` (or `client-delivery/START_HERE.md` for the assistant-guided setup).

## At a glance

| Need | Requirement |
|---|---|
| Computer | Windows 11 (tested). Windows 10, macOS and Linux should work but are untested. |
| Python | **3.10 or newer** (tested on 3.12.10) |
| Accounts | OpenAI API account with billing; Google account (Gmail + Drive) |
| Internet | Yes, outbound HTTPS |
| Browser | One-time, on the same computer, for the Google sign-in |
| Hardware | Nothing special. No GPU, server, Docker or database. |

## 1. The computer

- **OS:** the agent has been run on Windows 11. The code is plain Python, so Windows 10, macOS and Linux should work too, but none of those have been tested.
- **Hardware:** any ordinary laptop or desktop. A few hundred MB of free disk space is plenty.
- **Disk growth:** every invoice file is downloaded to `downloads/` and kept. Measured: about 0.35 MB per invoice (169 files = 59 MB). Delete old files there when you like; nothing reads them after processing.
- **It runs only when you run it.** The agent is a script (`python scripts/run_new_invoices.py`), not a background service. The computer only needs to be on at that moment.
- **A web browser is needed once**, on the same computer, to sign in to Google (see section 3).

## 2. Software

- **Python 3.10+.** The code uses `str | None` type syntax, which fails on 3.9 and older. On Windows, install from python.org and tick **"Add python.exe to PATH"** in the installer, then check with `python --version`.
- **Python packages**, installed with `pip install -r requirements.txt`:

  | Package | Used for |
  |---|---|
  | `openai` | reading invoices (PDFs and images) |
  | `google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib` | Gmail, Drive and Sheets |
  | `python-dotenv` | loading `.env` |

  Also install **`pymupdf`** (`pip install pymupdf`) if you use `run_new_invoices.py` or `rename_invoices.py`. They render PDF pages for the second rename read, and `requirements.txt` doesn't list it yet.
- **Tested versions:** openai 3.6.0, google-api-python-client 2.199.0, google-auth-oauthlib 1.4.1, python-dotenv 1.2.3, pymupdf 1.28.2. The code calls the OpenAI Responses API (`client.responses.create`), which needs a recent `openai` package, so don't pin an old one.
- **Optional:** VS Code plus an AI coding assistant, only for the guided setup in `client-delivery/START_HERE.md`. Git is only needed if you clone the repository.

## 3. Accounts and keys

**OpenAI**
- An OpenAI API account with **billing enabled**, and an API key from platform.openai.com/api-keys.
- Access to `gpt-4o`, the model used for extraction and renaming.
- Usage is billed per call: one extraction per invoice, plus two extra reads per new file if you use `run_new_invoices.py` for renaming. A run with nothing new makes no OpenAI calls.
- If you hit rate limits on a big batch, lower `MAX_CONCURRENT_EXTRACTIONS` in `.env` (default 4).

**Google**
- The Google account (personal Gmail or Workspace) whose mail and Drive folder the agent should read.
- A Google Cloud project with the **Gmail API, Drive API and Sheets API** enabled.
- An **OAuth client of type "Desktop app"**, saved as `credentials/oauth-client.json`. A service account will not work for Gmail intake.
- The agent asks for these permissions when you sign in: read Gmail, send Gmail (for the "needs review" alerts), full Drive, and Sheets.
- Run `python scripts/authorize.py` once. It opens a browser on that computer and saves `credentials/token.json`. A machine with no browser can't do this step; do it on a normal computer and copy `token.json` across.
- **Token expiry:** if the OAuth consent screen is left in **Testing** mode, Google expires the sign-in after 7 days and unattended runs start failing. Set the app to **In production**, where an unverified app just shows a warning you click through. A Workspace **Internal** app avoids this entirely. Check the current rules in the Google Cloud console.
- **Managed Workspace accounts:** an administrator may block third-party OAuth apps. If sign-in is refused, that is the likely cause.

## 4. Network

Outbound HTTPS to `api.openai.com`, `*.googleapis.com` and `accounts.google.com`. During sign-in, Google redirects back to `localhost` on a temporary port, so a local firewall must allow that.

## 5. Files the client must create and keep

| File or folder | What it is | Notes |
|---|---|---|
| `.env` | OpenAI key, Drive folder ID, review email, Gmail query | Copy from `.env.example` and fill in. Keep private. |
| `credentials/oauth-client.json` | Google OAuth client | Downloaded from Google Cloud. Keep private. |
| `credentials/token.json` | Google sign-in | Created by `authorize.py`. Keep private. |
| `state/` | Memory of what's already processed | Created automatically. **Must persist between runs**: if it is deleted, the agent treats every file as new and logs duplicates. |

`.gitignore` already excludes `.env`, `credentials/`, `state/` and `downloads/`. Don't share them or put them in a shared or public folder.

## 6. Quick check that a new machine is ready

```
python --version                      # 3.10 or newer
pip install -r requirements.txt pymupdf
python scripts/authorize.py           # browser opens once; prints your Drive folder IDs
python tests/test_validate_invoice.py # offline tests, no accounts needed (11 should pass)
python scripts/run_new_invoices.py    # rename new files, then log them
```
