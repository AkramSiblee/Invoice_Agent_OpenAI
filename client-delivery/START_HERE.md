# Getting started

This installs your Invoice Processing Agent — a tool that watches your Gmail
and/or a Google Drive folder for incoming invoices, reads them with AI,
logs them to a Google Sheet, and flags anything that needs a human look
before it's trusted.

Setup takes about 15-20 minutes and is done by *talking to an AI coding
assistant*, not by you writing or editing code yourself.

## What you'll need before you start

- A computer with [VS Code](https://code.visualstudio.com/) installed
- An AI coding assistant with access to your files and a terminal — any of
  these work: **Claude Code**, **Cursor**, **GitHub Copilot** (agent mode),
  or similar. If you don't already have one, Claude Code is a solid default:
  install it from [claude.com/product/claude-code](https://claude.com/product/claude-code)
  and sign in with a Claude account (Pro/Max) or an Anthropic API key.
- An **OpenAI API key** — the agent reads your invoices using OpenAI. Get one
  at [platform.openai.com/api-keys](https://platform.openai.com/api-keys) if
  you don't have one (you'll need billing enabled on the OpenAI account).
- The Google account (personal Gmail or Workspace) you want this to run
  against — you'll grant it read access to Gmail, and access to a Drive
  folder + two Google Sheets it creates for itself.

Nothing else needs to be installed manually — the setup prompt below installs
Python dependencies for you.

## Steps

1. Create a new, empty folder on your computer — call it whatever you like,
   e.g. `invoice-agent`.
2. Open that folder in VS Code (`File > Open Folder...`).
3. Open your AI coding assistant's chat/agent panel inside VS Code, pointed
   at this folder.
4. Open `SETUP_PROMPT.md` (the other file in this delivery), select all its
   contents, copy it, and paste it as your **very first message** to the
   assistant.
5. Send it, then just follow along. The assistant will:
   - build out the whole project file-by-file,
   - install dependencies,
   - ask you questions **one at a time** — your OpenAI key, which Google
     account/folder to use, where review alerts should go, etc. — and wait
     for each answer before moving on,
   - walk you through the one manual step Google requires (creating an
     OAuth client in Google Cloud Console — it'll give you exact clicks),
   - run a real test invoice through the whole pipeline so you can see it
     work before you're done.

Have your OpenAI key handy and expect one browser pop-up for Google sign-in
partway through.

## After setup

Once it's working, run it periodically with:

```
python scripts/main.py
```

Set that up on a schedule (Windows Task Scheduler, cron, or similar) so it
checks for new invoices automatically — the assistant can help you wire that
up too if you ask.

**Keep `.env` and the `credentials/` folder private.** They hold your API
key and Google access token. Never send them to anyone or commit them to a
public repository — `.gitignore` is already set up to exclude both.
