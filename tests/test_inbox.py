import email
import email.policy
import imaplib
import json
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import inbox  # noqa: E402


def _cfg(**over):
    base = dict(user="owner@icloud.com", from_addr="pat@example.com.au", name="Pat",
                send_as=["pat@example.com.au", "go@example.com.au"])
    base.update(over)
    return inbox.Config(**base)


def _raw(msg: EmailMessage) -> bytes:
    return msg.as_bytes()


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for the command paths under test."""

    def __init__(self, messages: dict[str, bytes], accepted_users=("owner",)):
        self.messages = messages
        self.accepted = set(accepted_users)
        self.logins: list[str] = []
        self.appended: list[tuple[str, str, bytes]] = []
        self.stored: list[tuple[str, str, str]] = []
        self.selected = None

    # connection
    def login(self, user, pw):
        self.logins.append(user)
        if user not in self.accepted:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Authentication failed")
        return "OK", [b"ok"]

    def logout(self):
        return "BYE", []

    # folders
    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" INBOX',
                      b'(\\HasNoChildren \\Drafts) "/" Drafts',
                      b'(\\HasNoChildren \\Sent) "/" "Sent Messages"']

    def select(self, folder, readonly=False):
        self.selected = folder
        return "OK", [b"3"]

    # messages
    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [" ".join(sorted(self.messages, key=int)).encode()]
        if cmd == "FETCH":
            uid = args[0]
            if uid not in self.messages:
                return "NO", [None]
            flags = b"UID " + uid.encode() + b" FLAGS (\\Seen) BODY[]"
            return "OK", [(flags, self.messages[uid]), b")"]
        if cmd == "STORE":
            self.stored.append((args[0], args[1], args[2]))
            return "OK", [b""]
        raise AssertionError(cmd)

    def append(self, folder, flags, _date, raw):
        self.appended.append((folder, flags, raw))
        return "OK", [b"APPENDUID 1 9"]


def _plain(subject="Hello", body="hi there", msgid="<abc@ex>"):
    m = EmailMessage()
    m["From"] = "Alice <alice@example.org>"
    m["To"] = "pat@example.com.au"
    m["Subject"] = subject
    m["Date"] = "Tue, 07 Oct 2026 10:00:00 +1100"
    m["Message-ID"] = msgid
    m.set_content(body)
    return m


# ---------------------------------------------------------------- parsing


def test_message_body_prefers_plain_over_html():
    m = _plain()
    m.add_alternative("<html><body><p>hi <b>there</b></p></body></html>", subtype="html")
    body, attachments = inbox.message_body(m)
    assert body == "hi there"
    assert attachments == []


def test_message_body_falls_back_to_html_text_and_lists_attachments():
    m = EmailMessage()
    m["Subject"] = "x"
    m.set_content("<html><style>p{}</style><body><p>Line one</p><p>Line &amp; two</p></body></html>",
                  subtype="html")
    m.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="cert.pdf")
    body, attachments = inbox.message_body(m)
    assert body == "Line one\nLine & two"
    assert attachments == [{"filename": "cert.pdf", "content_type": "application/pdf", "size": 8}]


def test_save_attachments_requires_fresh_dir_and_dedupes(tmp_path):
    m = EmailMessage()
    m["Subject"] = "x"
    m.set_content("body")
    m.add_attachment(b"a", maintype="text", subtype="csv", filename="../evil.csv")
    m.add_attachment(b"b", maintype="text", subtype="csv", filename="evil.csv")
    out = tmp_path / "mail"
    saved = inbox.save_attachments(m, out)
    assert [p.name for p in saved] == ["evil.csv", "evil-1.csv"]
    assert all(p.parent == out for p in saved)
    with pytest.raises(FileExistsError):
        inbox.save_attachments(m, out)


def test_summary_decodes_headers_and_flags():
    m = _plain(subject="=?utf-8?q?Caf=C3=A9_invoice?=")
    parsed = email.message_from_bytes(_raw(m), policy=email.policy.default)
    s = inbox.message_summary("7", parsed, ["\\Seen"])
    assert s["subject"] == "Café invoice"
    assert s["from"] == "Alice <alice@example.org>"
    assert s["date"].startswith("2026-10-07T10:00:00")


# ---------------------------------------------------------------- composing


def test_build_message_threads_a_reply():
    parent = _plain(msgid="<parent@ex>")
    parent["References"] = "<root@ex>"
    msg = inbox.build_message(_cfg(), ["alice@example.org"], "Re: Hello", "thanks", reply_to=parent)
    assert msg["From"] == "Pat <pat@example.com.au>"
    assert msg["In-Reply-To"] == "<parent@ex>"
    assert msg["References"] == "<root@ex> <parent@ex>"
    assert msg["Message-ID"].endswith("@example.com.au>")
    assert msg.get_content().strip() == "thanks"


def test_check_send_as_refuses_unlisted_from():
    with pytest.raises(SystemExit, match="refusing"):
        inbox.check_send_as(_cfg(from_addr="ceo@example.com.au"))
    inbox.check_send_as(_cfg(send_as=None))  # unrestricted when unset


# ---------------------------------------------------------------- login


def test_imap_login_tries_local_part_then_full_address(monkeypatch):
    fake = FakeIMAP({}, accepted_users=("owner@icloud.com",))
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    assert inbox.imap_connect(_cfg(), "pw") is fake
    assert fake.logins == ["owner", "owner@icloud.com"]


def test_imap_login_failure_exits_without_echoing_password(monkeypatch):
    fake = FakeIMAP({}, accepted_users=())
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    with pytest.raises(SystemExit) as exc:
        inbox.imap_connect(_cfg(), "s3cret-value")
    assert "s3cret-value" not in str(exc.value)


# ---------------------------------------------------------------- commands


@pytest.fixture
def wired(monkeypatch, tmp_path):
    msgs = {"1": _raw(_plain(subject="First", msgid="<one@ex>")),
            "2": _raw(_plain(subject="Second", body="read me", msgid="<two@ex>"))}
    fake = FakeIMAP(msgs)
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    monkeypatch.setattr(inbox, "load_config", lambda path=None: _cfg())
    monkeypatch.setattr(inbox, "get_secret", lambda name: "app-pw")
    return fake


def test_list_json_is_newest_first(wired, capsys):
    assert inbox.main(["list", "--json", "--limit", "5"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["uid"] for r in rows] == ["2", "1"]
    assert rows[0]["subject"] == "Second"


def test_read_prints_body(wired, capsys):
    assert inbox.main(["read", "2"]) == 0
    out = capsys.readouterr().out
    assert "Subject: Second" in out and "read me" in out


def test_draft_reply_derives_recipient_and_lands_in_drafts(wired, tmp_path, capsys):
    body = tmp_path / "b.txt"
    body.write_text("on it")
    assert inbox.main(["draft", "--reply-to", "2", "--body-file", str(body)]) == 0
    folder, flags, raw = wired.appended[0]
    assert folder == '"Drafts"' and "\\Draft" in flags
    drafted = email.message_from_bytes(raw, policy=email.policy.default)
    assert drafted["To"] == "alice@example.org"
    assert drafted["Subject"] == "Re: Second"
    assert drafted["In-Reply-To"] == "<two@ex>"
    assert "Review and send from Mail" in capsys.readouterr().out


def test_send_without_confirm_is_refused_and_touches_nothing(wired, capsys):
    rc = inbox.main(["send", "--to", "a@b.c", "--subject", "x", "--body-file", "/dev/null"])
    assert rc == inbox.EXIT_REFUSED
    assert wired.logins == [] and wired.appended == []
    assert "Refusing" in capsys.readouterr().err


def test_send_with_confirm_transmits_and_copies_to_sent(wired, monkeypatch, tmp_path, capsys):
    sent = []

    class FakeSMTP:
        def send_message(self, msg):
            sent.append(msg)

        def quit(self):
            pass

    monkeypatch.setattr(inbox, "smtp_connect", lambda cfg, pw: FakeSMTP())
    body = tmp_path / "b.txt"
    body.write_text("done")
    rc = inbox.main(["send", "--reply-to", "1", "--body-file", str(body), "--confirm"])
    assert rc == 0
    assert sent[0]["To"] == "alice@example.org"
    assert wired.appended[0][0] == '"Sent Messages"'
    assert ("1", "+FLAGS", "(\\Answered)") in wired.stored
    assert "Sent to alice@example.org as pat@example.com.au" in capsys.readouterr().out


def test_watch_once_replay_emits_json_lines(wired, capsys):
    assert inbox.main(["watch", "--once", "--replay", "--since", "2026-10-01"]) == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert [l["uid"] for l in lines] == ["1", "2"]


def test_missing_secret_names_the_keychain_command(wired, monkeypatch, capsys):
    monkeypatch.setattr(inbox, "get_secret", lambda name: None)
    assert inbox.main(["list"]) == inbox.EXIT_USAGE
    err = capsys.readouterr().err
    assert "keychain.py set ICLOUD_MAIL_APP_PASSWORD" in err


# ---------------------------------------------------------------- review follow-ups


def test_quote_escapes_imap_specials():
    assert inbox._quote("Sent Messages") == '"Sent Messages"'
    assert inbox._quote('a"b\\c') == '"a\\"b\\\\c"'


def test_criteria_sends_non_ascii_term_as_utf8_literal():
    class A:
        unseen = True; since = None; from_ = None; subject = "Café"
    assert inbox._criteria(A()) == ["CHARSET", "UTF-8", "UNSEEN", "SUBJECT", "Café".encode()]
    A.subject = "plain"
    assert inbox._criteria(A()) == ["UNSEEN", "SUBJECT", '"plain"']
    A.from_, A.subject = "Zoë", "Café"
    with pytest.raises(SystemExit, match="Only one"):
        inbox._criteria(A())


def test_search_uids_passes_bytes_as_imap_literal(wired):
    seen = {}
    orig = wired.uid

    def uid(cmd, *args):
        seen["literal"] = getattr(wired, "literal", None)
        seen["args"] = args
        return orig(cmd, *args)

    wired.uid = uid
    inbox.search_uids(wired, "INBOX", ["CHARSET", "UTF-8", "SUBJECT", "Café".encode()])
    assert seen == {"literal": "Café".encode(), "args": ("CHARSET", "UTF-8", "SUBJECT")}


@pytest.mark.parametrize("bad", ["a@b.c\r\nBcc: v@e.c", "not-an-address", "", "x@y.z\x00"])
def test_build_message_rejects_bad_addresses(bad):
    with pytest.raises(SystemExit, match="Not a usable address"):
        inbox.build_message(_cfg(), [bad], "s", "b")


def test_build_message_rejects_multiline_subject():
    with pytest.raises(SystemExit, match="line breaks"):
        inbox.build_message(_cfg(), ["a@b.c"], "s\r\nBcc: v@e.c", "b")


def test_auth_error_redacts_echoed_secret(monkeypatch):
    class Echoing(FakeIMAP):
        def login(self, user, pw):
            raise imaplib.IMAP4.error(f"NO bad credentials {pw}")
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", lambda *a, **k: Echoing({}))
    with pytest.raises(SystemExit) as exc:
        inbox.imap_connect(_cfg(), "hunter2")
    assert "hunter2" not in str(exc.value) and "[redacted]" in str(exc.value)


def test_mark_stores_the_right_flag(wired, capsys):
    assert inbox.main(["mark", "2", "--flag"]) == 0
    assert wired.stored == [("2", "+FLAGS", "(\\Flagged)")]
    assert inbox.main(["mark", "2", "--unseen"]) == 0
    assert wired.stored[-1] == ("2", "-FLAGS", "(\\Seen)")


def test_move_falls_back_to_copy_delete_expunge(wired, capsys):
    calls = []
    orig_uid = wired.uid

    def uid(cmd, *args):
        calls.append((cmd, *args))
        if cmd == "MOVE":
            return "NO", [b"unsupported"]
        if cmd == "COPY":
            return "OK", [b""]
        return orig_uid(cmd, *args)

    wired.uid = uid
    wired.expunge = lambda: calls.append(("EXPUNGE",))
    assert inbox.main(["move", "1", "--to", "Archive"]) == 0
    assert calls == [("MOVE", "1", '"Archive"'), ("COPY", "1", '"Archive"'),
                     ("STORE", "1", "+FLAGS", "(\\Deleted)"), ("EXPUNGE",)]


def test_configure_roundtrip_keeps_secret_out(tmp_path, monkeypatch, capsys):
    path = tmp_path / "inbox.json"
    monkeypatch.setattr(inbox, "CONFIG_PATH", path)
    rc = inbox.main(["configure", "--user", "o@icloud.com", "--from", "p@ex.au", "--name", "P",
                     "--send-as", "p@ex.au", "g@ex.au"])
    assert rc == 0
    data = json.loads(path.read_text())
    assert data["user"] == "o@icloud.com" and data["from_addr"] == "p@ex.au"
    assert data["send_as"] == ["p@ex.au", "g@ex.au"]
    assert data["imap_host"] == "imap.mail.me.com" and data["smtp_port"] == 587
    assert "password" not in path.read_text().lower()
    cfg = inbox.load_config(path)
    assert cfg.sender == "P <p@ex.au>"


def test_watch_logs_out_when_fetch_fails(wired, monkeypatch):
    outs = []
    wired.logout = lambda: outs.append("bye") or ("BYE", [])
    monkeypatch.setattr(inbox, "fetch_message", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert inbox.main(["watch", "--once", "--replay", "--since", "2026-10-01"]) == inbox.EXIT_AUTH
    assert outs == ["bye"]
