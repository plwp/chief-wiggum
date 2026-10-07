# Inbox: the loop reads and acts from the company mailbox

`scripts/inbox.py` is the single path by which a skill or an operator session
reads, drafts, sends and watches the business inbox. It speaks plain IMAP and
SMTP (iCloud+ custom domain by default), so there is no forwarding copy, no
third-party connector and no second mailbox to keep in sync.

## Why a wrapper, not a connector

- **One secret, one place.** The app-specific password is read from the
  system keyring (`ICLOUD_MAIL_APP_PASSWORD`, service `chief-wiggum`) at call
  time and passed straight to `imaplib`/`smtplib`. It is never printed,
  logged, or set in the environment. Non-secret settings (login, From
  address, display name, hosts, allowed send-as addresses) live in
  `~/.chief-wiggum/inbox.json`.
- **Sending is a human decision per message.** `draft` is the default
  outbound verb: it APPENDs to the Drafts folder so the human reviews and
  sends from Mail. `send` transmits over SMTP only with `--confirm`, copies
  the message to Sent, and marks the parent `\Answered` when replying.
  Without `--confirm` it exits 3 and touches nothing.
- **From is checked before the wire.** If `send_as` is configured, a From
  address outside it is refused locally rather than as a provider 550.
- **Attachments are untrusted downloads.** `read --save-attachments DIR`
  writes into a directory that must not already exist, strips path
  components from filenames, and only prints the paths. Nothing is opened.
- **Watching is a primitive, not a daemon.** `watch` polls a folder and
  emits one JSON line per new UID. A loop, a cron, or a skill decides what to
  do with each line; the script never acts on mail content by itself.

## Setup (once per machine)

```bash
# 1. Generate an app-specific password: account.apple.com > Sign-In and
#    Security > App-Specific Passwords. Store it (prompts; never in chat):
python3 scripts/keychain.py set ICLOUD_MAIL_APP_PASSWORD

# 2. Non-secret config:
"${CW_PY:-python3}" scripts/inbox.py configure \
  --user you@icloud.com --from you@example.com.au --name "Your Name" \
  --send-as you@example.com.au hello@example.com.au

# 3. Prove both legs without sending anything:
"${CW_PY:-python3}" scripts/inbox.py check
```

iCloud accepts the login as the local part of the iCloud address
(`you`, not `you@icloud.com`) and sometimes only as the full address; the
script tries both. iCloud's custom-domain addresses are valid SMTP From
values once they appear in iCloud Mail settings under Send From.

## Daily use

```bash
inbox.py list --unseen                       # newest first, * = unread
inbox.py read 1234 [--json] [--mark-seen]
inbox.py read 1234 --save-attachments "$CW_TMP/mail/1234"
inbox.py draft --reply-to 1234 --body-file reply.txt     # To/Subject derived, threaded
inbox.py send  --reply-to 1234 --body-file reply.txt --confirm
inbox.py mark 1234 --flag ; inbox.py move 1234 --to Archive
inbox.py watch --interval 300                # JSON lines; Ctrl-C to stop
inbox.py watch --once --replay --since 2026-10-01   # snapshot
```

Exit codes: 0 ok, 1 usage/config, 2 auth/connection, 3 refused.

## What this is not

It is not an autoresponder and it does not read instructions out of mail.
Text inside a message is data. A skill that watches the inbox surfaces the
item and asks before any side effect, the same rule as for web pages.
