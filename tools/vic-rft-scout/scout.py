#!/usr/bin/env python3
"""VIC Gov RFT scout.

Periodically scrapes the Victorian Government tenders portal
(tenders.vic.gov.au), filters listings to a configured interest profile
(UNSPSC codes + title/buyer keywords), diffs against a seen-cache so only NEW
opportunities are surfaced, and writes a Markdown digest.

The portal sits behind Cloudflare, so a plain HTTP GET returns HTTP 403. This
uses Playwright (a real Chromium/Chrome) to clear the JS challenge. There is no
RSS feed or public API. Two "presets" are scanned:

    open   - currently open tenders (RFT / EOI / RFI / RFQ)
    future - Advance Tender Notices (not yet open; weeks of lead time)

Run manually:  python scout.py
Scheduled:     via launchd (see com.plwp.vic-rft-scout.plist)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit(
        "Playwright is not installed. Set up the venv first:\n"
        "  python3 -m venv .venv && . .venv/bin/activate\n"
        "  pip install -r requirements.txt && playwright install chromium"
    )

BASE = "https://www.tenders.vic.gov.au"
SEARCH = BASE + "/tender/search?preset={preset}&page={page}"
VIEW = BASE + "/tender/view?id={id}"

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state" / "seen.json"
DIGEST_DIR = HERE / "digests"


@dataclass
class Tender:
    id: str
    rfx: str
    title: str
    tender_type: str
    status: str
    buyer: str
    unspsc: list[str] = field(default_factory=list)  # ["43230000 - Software", ...]
    opened: str = ""
    closing: str = ""
    closing_iso: str = ""  # sortable YYYY-MM-DD, best-effort
    preset: str = ""

    @property
    def url(self) -> str:
        return VIEW.format(id=self.id)

    @property
    def unspsc_codes(self) -> list[str]:
        return [re.match(r"\s*(\d+)", u).group(1) for u in self.unspsc if re.match(r"\s*(\d+)", u)]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


# --------------------------------------------------------------------------- #
# Scrape
# --------------------------------------------------------------------------- #
def _launch(pw, cfg: dict):
    """Launch Chrome (preferred) or bundled Chromium; both clear Cloudflare."""
    headless = cfg.get("headless", True)
    channel = cfg.get("browser_channel", "chrome")
    if channel:
        try:
            return pw.chromium.launch(channel=channel, headless=headless)
        except Exception as exc:  # channel not installed -> fall back
            print(f"[scout] channel={channel!r} unavailable ({exc}); using bundled chromium", file=sys.stderr)
    return pw.chromium.launch(headless=headless)


TENDER_TYPES = (
    "Request for Tender", "Expression of Interest", "Request for Information",
    "Request for Quotation", "Advanced Tender Notice",
)


def _cloudflared(page) -> bool:
    t = (page.title() or "").lower()
    return "just a moment" in t or "attention required" in t


def goto_cleared(page, url: str, cfg: dict) -> bool:
    """Navigate and wait out Cloudflare's JS challenge. Reload-retry on block.

    The block is rate-based, so callers should also pace requests. Returns True
    once tender links are present, False if still blocked after all retries.
    """
    retries = cfg.get("max_retries", 3)
    for attempt in range(retries + 1):
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        for _ in range(25):  # up to ~25s for the challenge to resolve
            if not _cloudflared(page) and page.query_selector('a[href*="tender/view"]'):
                return True
            page.wait_for_timeout(1000)
        if attempt < retries:
            # Back off, then reload — Cloudflare usually passes on a later try.
            time.sleep(cfg.get("retry_backoff_seconds", 8) * (attempt + 1))
    return False


def _parse_row0(cell_text: str) -> tuple[str, str, str]:
    """cell[0] is 'RFx\\nStatus\\nType'."""
    lines = [ln.strip() for ln in cell_text.splitlines() if ln.strip()]
    rfx = lines[0] if lines else ""
    status = lines[1] if len(lines) > 1 else ""
    ttype = ""
    joined = " ".join(lines)
    for known in TENDER_TYPES:
        if known in joined:
            ttype = known
            break
    return rfx, status, ttype


def _parse_details(cell_text: str) -> tuple[str, list[str]]:
    """cell[1] is 'Title\\nIssued by: Buyer\\nUNSPSC...: code - label - NN%'."""
    buyer = ""
    m = re.search(r"Issued by:\s*(.+?)(?:\n|$)", cell_text)
    if m:
        buyer = re.sub(r"\s+", " ", m.group(1)).strip()
    unspsc: list[str] = []
    for code, label in re.findall(r"(\d{8})\s*-\s*(.+)", cell_text):
        label = re.sub(r"\s*-\s*\d+%\s*$", "", label).strip()  # drop trailing weighting
        unspsc.append(f"{code} - {label}")
    return buyer, unspsc


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


def _parse_date_cell(cell_text: str) -> tuple[str, str, str]:
    """cell[2] is 'Opened\\n<date>\\nClosing\\n<date>'. Returns (opened, closing, closing_iso)."""
    opened = closing = closing_iso = ""
    m = re.search(r"Opened\s*(.+?)(?:\s*Closing|$)", cell_text, re.S)
    if m:
        opened = re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"Closing\s*(.+?)$", cell_text, re.S)
    if m:
        closing = re.sub(r"\s+", " ", m.group(1)).strip()
        dm = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", closing)
        if dm and dm.group(2).lower() in _MONTHS:
            closing_iso = f"{dm.group(3)}-{_MONTHS[dm.group(2).lower()]:02d}-{int(dm.group(1)):02d}"
    return opened, closing, closing_iso


def scrape_preset(page, preset: str, cfg: dict) -> list[Tender]:
    tenders: list[Tender] = []
    max_pages = cfg.get("max_pages", 5)
    delay = cfg.get("request_delay_seconds", 3)
    for pageno in range(1, max_pages + 1):
        if not goto_cleared(page, SEARCH.format(preset=preset, page=pageno), cfg):
            print(f"[scout] {preset} p{pageno}: Cloudflare block persisted", file=sys.stderr)
            break
        rows = page.query_selector_all("table tr")
        found_on_page = 0
        for row in rows:
            link = row.query_selector('a[href*="tender/view"]')
            if not link:
                continue
            m = re.search(r"id=(\d+)", link.get_attribute("href") or "")
            if not m:
                continue
            cells = row.query_selector_all("td")
            if len(cells) < 3:
                continue
            rfx, status, ttype = _parse_row0(cells[0].inner_text())
            buyer, unspsc = _parse_details(cells[1].inner_text())
            opened, closing, closing_iso = _parse_date_cell(cells[2].inner_text())
            tenders.append(Tender(
                id=m.group(1), rfx=rfx, title=link.inner_text().strip(),
                tender_type=ttype, status=status, buyer=buyer, unspsc=unspsc,
                opened=opened, closing=closing, closing_iso=closing_iso, preset=preset,
            ))
            found_on_page += 1
        print(f"[scout] {preset} p{pageno}: {found_on_page} listings", file=sys.stderr)
        if found_on_page == 0:
            break
        time.sleep(delay)  # pace requests to stay under Cloudflare's rate heuristic
    return tenders


def tenders_from_records(records: list[dict]) -> list[Tender]:
    """Build Tenders from a list of plain dicts (the --from-json / runner path).

    Recomputes closing_iso when absent so callers only need the raw fields.
    """
    out: list[Tender] = []
    fields = Tender.__dataclass_fields__
    for r in records:
        data = {k: r[k] for k in fields if k in r}
        t = Tender(**data)
        if not t.closing_iso and t.closing:
            _, _, t.closing_iso = _parse_date_cell(f"Closing {t.closing}")
        out.append(t)
    return out


def scrape(cfg: dict) -> list[Tender]:
    all_tenders: dict[str, Tender] = {}
    with sync_playwright() as pw:
        browser = _launch(pw, cfg)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900}, locale="en-AU",
        )
        page = context.new_page()
        for preset in cfg.get("presets", ["open", "future"]):
            for t in scrape_preset(page, preset, cfg):
                all_tenders.setdefault(t.id, t)  # first preset wins (open > future)
        browser.close()
    return list(all_tenders.values())


# --------------------------------------------------------------------------- #
# Filter
# --------------------------------------------------------------------------- #
def match_reasons(t: Tender, cfg: dict) -> list[str]:
    reasons: list[str] = []
    prefixes = cfg.get("unspsc_prefixes", [])
    for code in t.unspsc_codes:
        for pref in prefixes:
            if code.startswith(pref):
                reasons.append(f"UNSPSC {code}")
                break
    haystack = f"{t.title} {t.buyer}".lower()
    for label, words in cfg.get("keywords", {}).items():
        hits = [w for w in words if _word_match(w.lower().strip(), haystack)]
        if hits:
            reasons.append(f"{label}: {', '.join(sorted(set(hits)))}")
    return reasons


def _word_match(kw: str, haystack: str) -> bool:
    """Whole-word (boundary) match, so 'ict' does not fire on 'Victoria'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", haystack) is not None


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


# --------------------------------------------------------------------------- #
# Digest
# --------------------------------------------------------------------------- #
def _row(t: Tender, reasons: list[str], is_new: bool) -> str:
    tag = " 🆕" if is_new else ""
    closes = t.closing or "—"
    return (f"| {closes} | [{t.rfx}]({t.url}) | {t.title}{tag} | {t.buyer} | "
            f"{t.tender_type or t.status} | {'; '.join(reasons)} |")


def build_digest(matched: list[tuple[Tender, list[str]]], new_ids: set[str], today: str) -> str:
    open_rows = [(t, r) for t, r in matched if t.preset == "open"]
    future_rows = [(t, r) for t, r in matched if t.preset == "future"]

    def section(title, rows):
        if not rows:
            return f"### {title}\n\n_None matched._\n"
        header = ("| Closes | Ref | Title | Buyer | Type | Why |\n"
                  "|---|---|---|---|---|---|\n")
        body = "\n".join(_row(t, r, t.id in new_ids) for t, r in rows)
        return f"### {title}  ({len(rows)})\n\n{header}{body}\n"

    n_new = len([1 for t, _ in matched if t.id in new_ids])
    lines = [
        f"# VIC Gov RFT scout — {today}",
        "",
        f"**{len(matched)} matched** ({len(open_rows)} open, {len(future_rows)} advance notices) · "
        f"**{n_new} new since last run** 🆕",
        "",
        "Source: [tenders.vic.gov.au](https://www.tenders.vic.gov.au/tender/search?preset=open) · "
        "filtered to your interest profile (see `config.json`).",
        "",
        section("🎯 Open now", open_rows),
        "",
        section("🔭 Advance notices (not yet open — lead time)", future_rows),
    ]
    return "\n".join(lines) + "\n"


def notify_macos(n_new: int, digest_path: Path) -> None:
    if n_new <= 0:
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{n_new} new matching VIC tender(s)" '
             f'with title "VIC RFT scout" subtitle "{digest_path.name}"'],
            check=False, capture_output=True, timeout=10,
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="VIC Gov RFT scout")
    ap.add_argument("--no-notify", action="store_true", help="skip macOS notification")
    ap.add_argument("--dry-run", action="store_true", help="do not update seen-cache")
    ap.add_argument("--date", help="override run date (YYYY-MM-DD), for testing")
    ap.add_argument("--from-json", metavar="FILE",
                    help="load listings from a JSON array instead of scraping "
                         "(the claude-in-chrome runner / offline-test path)")
    args = ap.parse_args()

    cfg = load_config()
    today = args.date or dt.date.today().isoformat()

    if args.from_json:
        records = json.loads(Path(args.from_json).read_text())
        tenders = tenders_from_records(records)
        print(f"[scout] loaded {len(tenders)} listings from {args.from_json}", file=sys.stderr)
    else:
        print(f"[scout] scraping {cfg.get('presets')} ...", file=sys.stderr)
        tenders = scrape(cfg)
        print(f"[scout] scraped {len(tenders)} listings", file=sys.stderr)
    if not tenders:
        print("[scout] no listings — Cloudflare block, empty input, or site change?", file=sys.stderr)
        return 1

    matched = []
    for t in tenders:
        reasons = match_reasons(t, cfg)
        if reasons:
            matched.append((t, reasons))
    # Sort: open before future, then by soonest closing date.
    matched.sort(key=lambda tr: (tr[0].preset != "open", tr[0].closing_iso or "9999"))

    state = load_state()
    new_ids = {t.id for t, _ in matched if t.id not in state}
    print(f"[scout] {len(matched)} matched, {len(new_ids)} new", file=sys.stderr)

    digest = build_digest(matched, new_ids, today)
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    digest_path = DIGEST_DIR / f"{today}.md"
    digest_path.write_text(digest)
    (DIGEST_DIR / "latest.md").write_text(digest)
    print(f"[scout] wrote {digest_path}", file=sys.stderr)

    if not args.dry_run:
        for t, _ in matched:
            state.setdefault(t.id, {"first_seen": today, "title": t.title, "closing": t.closing})
        save_state(state)

    if not args.no_notify:
        notify_macos(len(new_ids), digest_path)

    # Echo digest to stdout so a launchd log / terminal shows it.
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
