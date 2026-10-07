#!/usr/bin/env python3
"""Inbox: read, draft, send and watch a company inbox over IMAP/SMTP.

The operator's business inbox (iCloud+ custom domain by default) is a place the
loop has to be able to look at and act from: supplier onboarding mail, portal
notices, client replies. This is the single wrapper every skill uses for that,
so the rules about secrets and about sending live in one file.

Rules:

- The app-specific password is fetched from the system keyring
  (``ICLOUD_MAIL_APP_PASSWORD`` under the ``chief-wiggum`` service) at call
  time and handed to ``imaplib``/``smtplib``; it is never printed, logged, or
  placed in an environment variable. Non-secret settings (login, From address,
  display name, hosts) live in ``~/.chief-wiggum/inbox.json``.
- ``draft`` is the default outbound verb: it APPENDs a message to the Drafts
  folder so the human reviews and sends from Mail. ``send`` transmits over
  SMTP and refuses without ``--confirm``; sending on someone's behalf is a
  per-message human decision, not a default.
- Attachments are untrusted downloads. ``read --save-attachments DIR`` writes
  them into DIR (created fresh) and prints the paths; nothing is opened or
  executed.
- ``watch`` is the monitor primitive: it polls a folder and emits one JSON
  line per new message, so a loop or cron can wake on mail without a second
  copy of the mailbox living anywhere else.

Exit codes: 0 ok; 1 usage/config error; 2 auth/connection failure;
3 refused (send without --confirm, From address not on the account).

Usage::

    python3 inbox.py configure --user you@icloud.com --from you@example.com --name "You"
    python3 inbox.py check
    python3 inbox.py list --unseen --limit 20 [--json]
    python3 inbox.py read 1234 [--save-attachments "$CW_TMP/mail/1234"]
    python3 inbox.py draft --to a@b.c --subject "Re: x" --body-file reply.md [--reply-to 1234]
    python3 inbox.py send  --to a@b.c --subject "x" --body-file msg.md --confirm
    python3 inbox.py mark 1234 --seen | --unseen | --flag | --unflag
    python3 inbox.py move 1234 --to Archive
    python3 inbox.py watch --interval 300
"""

from __future__ import annotations

import argparse
import email
import email.policy
import email.utils
import html
import imaplib
import json
import re
import smtplib
import ssl
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from keychain import get_secret  # noqa: E402

SECRET_NAME = "ICLOUD_MAIL_APP_PASSWORD"
CONFIG_PATH = Path.home() / ".chief-wiggum" / "inbox.json"

PROVIDERS = {
    "icloud": {
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
    },
}

EXIT_OK, EXIT_USAGE, EXIT_AUTH, EXIT_REFUSED = 0, 1, 2, 3


@dataclass
class Config:
    user: str
    from_addr: str
    name: str = ""
    imap_host: str = PROVIDERS["icloud"]["imap_host"]
    imap_port: int = PROVIDERS["icloud"]["imap_port"]
    smtp_host: str = PROVIDERS["icloud"]["smtp_host"]
    smtp_port: int = PROVIDERS["icloud"]["smtp_port"]
    # Addresses this account may send as. Checked before any SMTP call so a
    # typo in --from fails here, not as a 550 from the provider.
    send_as: list[str] | None = None

    @property
    def sender(self) -> str:
        return email.utils.formataddr((self.name, self.from_addr)) if self.name else self.from_addr


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        raise SystemExit(
            f"No inbox config at {path}. Run: inbox.py configure --user ... --from ..."
        )
    data = json.loads(path.read_text())
    return Config(**data)


def save_config(cfg: Config, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), indent=2) + "\n")


def _password() -> str:
    secret = get_secret(SECRET_NAME)
    if not secret:
        raise SystemExit(
            f"{SECRET_NAME} is not in the keyring. Generate an app-specific password "
            f"(account.apple.com > Sign-In and Security > App-Specific Passwords) and store it "
            f"with: python3 scripts/keychain.py set {SECRET_NAME}"
        )
    return secret.strip()


def _login_candidates(user: str) -> list[str]:
    """iCloud documents the username as the local part of the iCloud address,
    with the full address as the fallback; try both, local part first."""
    local = user.split("@", 1)[0]
    return [local, user] if local != user else [user]


def _redact(exc: Exception | None, secret: str) -> str:
    """A server may echo the credential in its failure line; never relay it."""
    return str(exc).replace(secret, "[redacted]") if exc else ""


def _quote(value: str) -> str:
    """IMAP quoted string: backslash-escape the two characters that end one."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_connect(cfg: Config, password: str) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ssl.create_default_context())
    last: Exception | None = None
    for candidate in _login_candidates(cfg.user):
        try:
            conn.login(candidate, password)
            return conn
        except imaplib.IMAP4.error as exc:  # wrong username form; try the next
            last = exc
    conn.logout()
    raise SystemExit(f"IMAP login failed for {cfg.user} (tried local part and full address): "
                     f"{_redact(last, password)}")


def smtp_connect(cfg: Config, password: str) -> smtplib.SMTP:
    conn = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60)
    conn.ehlo()
    conn.starttls(context=ssl.create_default_context())
    conn.ehlo()
    last: Exception | None = None
    for candidate in _login_candidates(cfg.user):
        try:
            conn.login(candidate, password)
            return conn
        except smtplib.SMTPAuthenticationError as exc:
            last = exc
    conn.quit()
    raise SystemExit(f"SMTP login failed for {cfg.user}: {_redact(last, password)}")


# ----------------------------------------------------------------------------
# Folders


def list_folders(conn: imaplib.IMAP4) -> list[dict]:
    status, rows = conn.list()
    if status != "OK":
        raise SystemExit(f"LIST failed: {status}")
    out = []
    for raw in rows or []:
        if raw is None:
            continue
        line = raw.decode() if isinstance(raw, bytes) else str(raw)
        m = re.match(r'\((?P<flags>[^)]*)\) "(?P<delim>[^"]*)" (?P<name>.+)$', line)
        if not m:
            continue
        name = m.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        out.append({"name": name, "flags": m.group("flags").split()})
    return out


def special_folder(conn: imaplib.IMAP4, use: str, fallbacks: tuple[str, ...]) -> str:
    """Resolve a special-use folder (\\Drafts, \\Sent) by flag, then by name."""
    folders = list_folders(conn)
    for f in folders:
        if use in f["flags"]:
            return f["name"]
    names = {f["name"] for f in folders}
    for fb in fallbacks:
        if fb in names:
            return fb
    raise SystemExit(f"No folder with {use} and none of {fallbacks} exist; folders: {sorted(names)}")


# ----------------------------------------------------------------------------
# Reading


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


class _TextExtractor(HTMLParser):
    _skip = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, _attrs):
        if tag in self._skip:
            self._depth += 1
        elif tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._skip and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if not self._depth:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    p = _TextExtractor()
    p.feed(markup)
    text = html.unescape("".join(p.parts))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def message_summary(uid: str, msg: EmailMessage, flags: list[str]) -> dict:
    parsed = email.utils.parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
    return {
        "uid": uid,
        "date": parsed.isoformat() if parsed else "",
        "from": _decode(msg["From"]),
        "to": _decode(msg["To"]),
        "subject": _decode(msg["Subject"]),
        "flags": flags,
        "message_id": msg["Message-ID"] or "",
    }


def _payload_bytes(part) -> bytes:
    raw = part.get_payload(decode=True)
    return raw if isinstance(raw, bytes) else b""


def message_body(msg: EmailMessage) -> tuple[str, list[dict]]:
    """Return (text, attachments). Prefers text/plain; falls back to html."""
    plain: list[str] = []
    rich: list[str] = []
    attachments: list[dict] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        ctype = part.get_content_type()
        if disposition == "attachment" or (filename and ctype not in {"text/plain", "text/html"}):
            attachments.append({"filename": _decode(filename) or "unnamed", "content_type": ctype,
                                "size": len(_payload_bytes(part))})
            continue
        try:
            content = part.get_content()
        except Exception:
            content = _payload_bytes(part).decode("utf-8", "replace")
        if ctype == "text/plain":
            plain.append(content)
        elif ctype == "text/html":
            rich.append(content)
    if plain:
        return "\n".join(plain).strip(), attachments
    if rich:
        return html_to_text("\n".join(rich)), attachments
    return "", attachments


def save_attachments(msg: EmailMessage, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=False)
    saved = []
    for part in msg.walk():
        if part.is_multipart() or not part.get_filename():
            continue
        name = Path(_decode(part.get_filename())).name or "unnamed"
        target = out_dir / name
        n = 1
        while target.exists():
            target = out_dir / f"{Path(name).stem}-{n}{Path(name).suffix}"
            n += 1
        target.write_bytes(_payload_bytes(part))
        saved.append(target)
    return saved


def _parse_flags(fetch_meta: bytes) -> list[str]:
    m = re.search(rb"FLAGS \(([^)]*)\)", fetch_meta)
    return m.group(1).decode().split() if m else []


def fetch_message(conn: imaplib.IMAP4, uid: str, headers_only: bool = False) -> tuple[EmailMessage, list[str]]:
    what = "(FLAGS BODY.PEEK[HEADER])" if headers_only else "(FLAGS BODY.PEEK[])"
    status, data = conn.uid("FETCH", uid, what)
    if status != "OK" or not data or data[0] is None:
        raise SystemExit(f"UID {uid} not found")
    meta, raw = data[0][0], data[0][1]
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return msg, _parse_flags(meta)


def search_uids(conn: imaplib.IMAP4, folder: str, criteria: list[str | bytes]) -> list[str]:
    """criteria may end in one ``bytes`` item: it is sent as an IMAP literal,
    which is how a non-ASCII search term reaches a server without UTF8=ACCEPT
    (iCloud has neither ENABLE nor UTF8=ACCEPT; imaplib would otherwise fail
    encoding the command as ASCII)."""
    status, _ = conn.select(_quote(folder), readonly=True)
    if status != "OK":
        raise SystemExit(f"Cannot open folder {folder!r}")
    words: list[str] = []
    for c in criteria:
        if isinstance(c, bytes):
            conn.literal = c
        else:
            words.append(c)
    status, data = conn.uid("SEARCH", *words)
    if status != "OK":
        raise SystemExit(f"SEARCH failed: {status}")
    return data[0].decode().split() if data and data[0] else []


def _imap_date(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%d-%b-%Y")


# ----------------------------------------------------------------------------
# Composing


def _check_addresses(addrs: list[str]) -> None:
    for a in addrs:
        name, addr = email.utils.parseaddr(a)
        if not addr or "@" not in addr or any(ch in a for ch in "\r\n\x00"):
            raise SystemExit(f"Not a usable address: {a!r}")


def build_message(cfg: Config, to: list[str], subject: str, body: str, *, cc: list[str] | None = None,
                  reply_to: EmailMessage | None = None) -> EmailMessage:
    _check_addresses([*to, *(cc or [])])
    if any(ch in subject for ch in "\r\n"):
        raise SystemExit("Subject may not contain line breaks")
    msg = EmailMessage()
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=cfg.from_addr.split("@", 1)[1])
    if reply_to is not None and reply_to["Message-ID"]:
        parent = reply_to["Message-ID"]
        msg["In-Reply-To"] = parent
        refs = (reply_to["References"] or "").split()
        msg["References"] = " ".join([*refs, parent])
    msg.set_content(body)
    return msg


def check_send_as(cfg: Config) -> None:
    if cfg.send_as and cfg.from_addr not in cfg.send_as:
        raise SystemExit(f"{cfg.from_addr} is not in send_as {cfg.send_as}; refusing")


def append_message(conn: imaplib.IMAP4, folder: str, msg: EmailMessage, flags: str) -> None:
    status, _ = conn.append(_quote(folder), flags, imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    if status != "OK":
        raise SystemExit(f"APPEND to {folder} failed: {status}")


# ----------------------------------------------------------------------------
# Commands


def _emit(rows, as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    for r in rows:
        flag = "*" if "\\Seen" not in r["flags"] else " "
        print(f"{flag} {r['uid']:>6}  {r['date'][:16]:16}  {r['from'][:32]:32}  {r['subject']}")


def cmd_configure(args) -> int:
    base = PROVIDERS[args.provider]
    cfg = Config(user=args.user, from_addr=args.from_addr, name=args.name or "",
                 imap_host=args.imap_host or base["imap_host"], imap_port=args.imap_port or base["imap_port"],
                 smtp_host=args.smtp_host or base["smtp_host"], smtp_port=args.smtp_port or base["smtp_port"],
                 send_as=args.send_as or None)
    save_config(cfg, CONFIG_PATH)
    print(f"Wrote {CONFIG_PATH} (login {cfg.user}, sending as {cfg.from_addr}). "
          f"Secret stays in the keyring as {SECRET_NAME}.")
    return EXIT_OK


def cmd_check(_args) -> int:
    cfg = load_config()
    pw = _password()
    try:
        conn = imap_connect(cfg, pw)
    except SystemExit as exc:
        print(f"imap: FAIL {exc}", file=sys.stderr)
        return EXIT_AUTH
    folders = [f["name"] for f in list_folders(conn)]
    conn.logout()
    print(f"imap: ok ({len(folders)} folders: {', '.join(folders[:8])}{'...' if len(folders) > 8 else ''})")
    try:
        smtp = smtp_connect(cfg, pw)
        smtp.quit()
    except SystemExit as exc:
        print(f"smtp: FAIL {exc}", file=sys.stderr)
        return EXIT_AUTH
    print(f"smtp: ok (would send as {cfg.sender})")
    return EXIT_OK


def cmd_folders(args) -> int:
    cfg = load_config()
    conn = imap_connect(cfg, _password())
    rows = list_folders(conn)
    conn.logout()
    print(json.dumps(rows, indent=2) if args.json else "\n".join(f["name"] for f in rows))
    return EXIT_OK


def _criteria(args) -> list[str | bytes]:
    """Build SEARCH criteria. At most one text term may be non-ASCII; it goes
    last as a UTF-8 literal with CHARSET UTF-8 in front."""
    crit: list[str | bytes] = []
    if getattr(args, "unseen", False):
        crit.append("UNSEEN")
    if getattr(args, "since", None):
        crit += ["SINCE", _imap_date(args.since)]
    terms = [(k, v) for k, v in (("FROM", getattr(args, "from_", None)), ("SUBJECT", getattr(args, "subject", None))) if v]
    non_ascii = [t for t in terms if not t[1].isascii()]
    if len(non_ascii) > 1:
        raise SystemExit("Only one of --from/--subject may contain non-ASCII characters")
    for k, v in terms:
        if v.isascii():
            crit += [k, _quote(v)]
    if non_ascii:
        k, v = non_ascii[0]
        crit = ["CHARSET", "UTF-8", *crit, k, v.encode("utf-8")]
    return crit or ["ALL"]


def cmd_list(args) -> int:
    cfg = load_config()
    conn = imap_connect(cfg, _password())
    uids = search_uids(conn, args.folder, _criteria(args))
    uids = uids[-args.limit:] if args.limit else uids
    rows = []
    for uid in reversed(uids):
        msg, flags = fetch_message(conn, uid, headers_only=True)
        rows.append(message_summary(uid, msg, flags))
    conn.logout()
    _emit(rows, args.json)
    return EXIT_OK


def cmd_read(args) -> int:
    cfg = load_config()
    conn = imap_connect(cfg, _password())
    conn.select(_quote(args.folder), readonly=not args.mark_seen)
    msg, flags = fetch_message(conn, args.uid)
    if args.mark_seen:
        conn.uid("STORE", args.uid, "+FLAGS", "(\\Seen)")
    conn.logout()
    if args.raw:
        sys.stdout.write(msg.as_string())
        return EXIT_OK
    summary = message_summary(args.uid, msg, flags)
    body, attachments = message_body(msg)
    saved = []
    if args.save_attachments:
        saved = [str(p) for p in save_attachments(msg, Path(args.save_attachments))]
    if args.json:
        print(json.dumps({**summary, "body": body, "attachments": attachments, "saved": saved}, indent=2))
        return EXIT_OK
    for k in ("from", "to", "date", "subject"):
        print(f"{k.capitalize()}: {summary[k]}")
    if attachments:
        print("Attachments: " + ", ".join(f"{a['filename']} ({a['content_type']}, {a['size']} B)" for a in attachments))
    if saved:
        print("Saved: " + ", ".join(saved))
    print()
    print(body)
    return EXIT_OK


def _compose(args, cfg: Config, conn: imaplib.IMAP4) -> EmailMessage:
    body = Path(args.body_file).read_text() if args.body_file else sys.stdin.read()
    parent = None
    if args.reply_to:
        conn.select(_quote(args.folder), readonly=True)
        parent, _ = fetch_message(conn, args.reply_to, headers_only=True)
        if not args.to:
            reply_addr = parent["Reply-To"] or parent["From"]
            args.to = [email.utils.parseaddr(reply_addr)[1]]
        if not args.subject:
            subj = _decode(parent["Subject"])
            args.subject = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    if not args.to or not args.subject:
        raise SystemExit("--to and --subject are required (or --reply-to to derive them)")
    return build_message(cfg, args.to, args.subject, body, cc=args.cc, reply_to=parent)


def cmd_draft(args) -> int:
    cfg = load_config()
    check_send_as(cfg)
    conn = imap_connect(cfg, _password())
    msg = _compose(args, cfg, conn)
    drafts = special_folder(conn, "\\Drafts", ("Drafts",))
    append_message(conn, drafts, msg, "(\\Draft \\Seen)")
    conn.logout()
    print(f"Draft saved to {drafts!r}: to={msg['To']} subject={msg['Subject']!r}. Review and send from Mail.")
    return EXIT_OK


def cmd_send(args) -> int:
    if not args.confirm:
        print("Refusing to send without --confirm. Use `draft` to stage it for human review instead.",
              file=sys.stderr)
        return EXIT_REFUSED
    cfg = load_config()
    check_send_as(cfg)
    pw = _password()
    conn = imap_connect(cfg, pw)
    msg = _compose(args, cfg, conn)
    smtp = smtp_connect(cfg, pw)
    try:
        smtp.send_message(msg)
    finally:
        smtp.quit()
    sent = special_folder(conn, "\\Sent", ("Sent Messages", "Sent"))
    append_message(conn, sent, msg, "(\\Seen)")
    if args.reply_to:
        conn.select(_quote(args.folder))
        conn.uid("STORE", args.reply_to, "+FLAGS", "(\\Answered)")
    conn.logout()
    print(f"Sent to {msg['To']} as {cfg.from_addr}; copy in {sent!r}; Message-ID {msg['Message-ID']}")
    return EXIT_OK


def cmd_mark(args) -> int:
    cfg = load_config()
    conn = imap_connect(cfg, _password())
    conn.select(_quote(args.folder))
    op, flag = {"seen": ("+FLAGS", "\\Seen"), "unseen": ("-FLAGS", "\\Seen"),
                "flag": ("+FLAGS", "\\Flagged"), "unflag": ("-FLAGS", "\\Flagged")}[args.state]
    status, _ = conn.uid("STORE", args.uid, op, f"({flag})")
    conn.logout()
    print(f"{args.uid}: {args.state} ({status})")
    return EXIT_OK if status == "OK" else EXIT_USAGE


def cmd_move(args) -> int:
    cfg = load_config()
    conn = imap_connect(cfg, _password())
    conn.select(_quote(args.folder))
    status, _ = conn.uid("MOVE", args.uid, _quote(args.to))
    if status != "OK":  # servers without MOVE: copy + delete + expunge
        status, _ = conn.uid("COPY", args.uid, _quote(args.to))
        if status == "OK":
            conn.uid("STORE", args.uid, "+FLAGS", "(\\Deleted)")
            conn.expunge()
    conn.logout()
    print(f"{args.uid}: moved to {args.to} ({status})")
    return EXIT_OK if status == "OK" else EXIT_USAGE


def cmd_watch(args) -> int:
    cfg = load_config()
    pw = _password()
    seen: set[str] | None = None
    rounds = 0
    while True:
        conn = imap_connect(cfg, pw)
        try:
            uids = set(search_uids(conn, args.folder, ["SINCE", _imap_date(args.since or date.today().isoformat())]))
            if seen is None:
                seen = uids if not args.replay else set()
            for uid in sorted(uids - seen, key=int):
                msg, flags = fetch_message(conn, uid, headers_only=True)
                print(json.dumps(message_summary(uid, msg, flags)), flush=True)
            seen |= uids
        finally:
            conn.logout()
        rounds += 1
        if args.once or (args.max_rounds and rounds >= args.max_rounds):
            return EXIT_OK
        time.sleep(args.interval)


# ----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("configure", help="write ~/.chief-wiggum/inbox.json (no secrets)")
    c.add_argument("--user", required=True, help="account login, e.g. you@icloud.com")
    c.add_argument("--from", dest="from_addr", required=True, help="default From address")
    c.add_argument("--name", help="display name")
    c.add_argument("--provider", choices=sorted(PROVIDERS), default="icloud")
    c.add_argument("--imap-host"); c.add_argument("--imap-port", type=int)
    c.add_argument("--smtp-host"); c.add_argument("--smtp-port", type=int)
    c.add_argument("--send-as", nargs="*", help="addresses this account may send as")
    c.set_defaults(func=cmd_configure)

    sub.add_parser("check", help="verify IMAP and SMTP login; sends nothing").set_defaults(func=cmd_check)

    f = sub.add_parser("folders"); f.add_argument("--json", action="store_true"); f.set_defaults(func=cmd_folders)

    ls = sub.add_parser("list", help="list messages (newest first)")
    ls.add_argument("--folder", default="INBOX")
    ls.add_argument("--unseen", action="store_true")
    ls.add_argument("--since", help="YYYY-MM-DD")
    ls.add_argument("--from", dest="from_")
    ls.add_argument("--subject")
    ls.add_argument("--limit", type=int, default=25)
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    rd = sub.add_parser("read", help="print one message")
    rd.add_argument("uid")
    rd.add_argument("--folder", default="INBOX")
    rd.add_argument("--raw", action="store_true", help="full RFC822 source")
    rd.add_argument("--json", action="store_true")
    rd.add_argument("--mark-seen", action="store_true")
    rd.add_argument("--save-attachments", metavar="DIR", help="save attachments into a NEW directory")
    rd.set_defaults(func=cmd_read)

    for name, func, helptext in (("draft", cmd_draft, "stage a message in Drafts for human review"),
                                 ("send", cmd_send, "send over SMTP (requires --confirm)")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--to", nargs="*")
        s.add_argument("--cc", nargs="*")
        s.add_argument("--subject")
        s.add_argument("--body-file", help="plain-text body; stdin if omitted")
        s.add_argument("--reply-to", metavar="UID", help="thread onto this message; derives --to/--subject")
        s.add_argument("--folder", default="INBOX", help="folder of --reply-to")
        if name == "send":
            s.add_argument("--confirm", action="store_true", help="the human approved this exact message")
        s.set_defaults(func=func)

    mk = sub.add_parser("mark"); mk.add_argument("uid"); mk.add_argument("--folder", default="INBOX")
    g = mk.add_mutually_exclusive_group(required=True)
    for st in ("seen", "unseen", "flag", "unflag"):
        g.add_argument(f"--{st}", dest="state", action="store_const", const=st)
    mk.set_defaults(func=cmd_mark)

    mv = sub.add_parser("move"); mv.add_argument("uid"); mv.add_argument("--folder", default="INBOX")
    mv.add_argument("--to", required=True); mv.set_defaults(func=cmd_move)

    w = sub.add_parser("watch", help="poll a folder; one JSON line per new message")
    w.add_argument("--folder", default="INBOX")
    w.add_argument("--interval", type=int, default=300, help="seconds between polls")
    w.add_argument("--since", help="YYYY-MM-DD window (default: today)")
    w.add_argument("--replay", action="store_true", help="emit messages already present on the first poll")
    w.add_argument("--once", action="store_true", help="single poll (use with --replay for a snapshot)")
    w.add_argument("--max-rounds", type=int, default=0)
    w.set_defaults(func=cmd_watch)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return EXIT_USAGE
        raise
    except (imaplib.IMAP4.error, smtplib.SMTPException, OSError) as exc:
        print(f"mail error: {exc}", file=sys.stderr)
        return EXIT_AUTH


if __name__ == "__main__":
    sys.exit(main())
