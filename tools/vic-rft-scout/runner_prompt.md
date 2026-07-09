# VIC RFT scout — claude-in-chrome runner

This is the **reliable fetch path**: it drives your real Chrome (which already
holds a warm Cloudflare clearance) via the Claude-in-Chrome extension, so it
doesn't get bot-challenged the way a fresh headless browser can.

Feed this file to a Claude Code session (e.g. `claude -p "$(cat runner_prompt.md)"`).

---

You are fetching Victorian Government tender listings so the local scout can
build a digest. Do exactly this, then stop:

1. Using the Claude-in-Chrome tools, open a tab and load each of these URLs in
   turn, waiting for results to render (the page title becomes "Current Tenders"
   or "Advance Tender Notice", not "Attention Required"). Pace ~3s between loads.
   - Open tenders, all pages:
     `https://www.tenders.vic.gov.au/tender/search?preset=open&page=1` … keep
     incrementing `page` until a page shows no `tender/view` links (currently ~3 pages).
   - Advance notices, all pages:
     `https://www.tenders.vic.gov.au/tender/search?preset=future&page=1` … same,
     until empty (currently ~3 pages).

2. For every result row on every page, extract these fields (use `javascript_tool`
   to read the table rows; each row has an `<a href=".../tender/view?id=NNNNN">`):
   - `id`   — the digits from the view link's `id=`
   - `rfx`  — the RFx number (first line of the row's first cell)
   - `status`, `tender_type` — from the first cell ("Open"/"Advance Notice";
     one of Request for Tender / Expression of Interest / Request for Information /
     Request for Quotation / Advanced Tender Notice)
   - `title` — the link text
   - `buyer` — the text after "Issued by:"
   - `unspsc` — a list of "CODE - label" strings (drop the trailing "- NN%")
   - `opened`, `closing` — the date strings
   - `preset` — "open" for the open pages, "future" for the advance-notice pages

3. Write the combined array of row objects to `rows.json` in this folder.

4. Run: `python scout.py --from-json rows.json`
   (activate `.venv` first). That filters to the interest profile, diffs against
   the seen-cache, writes `digests/<today>.md` + `digests/latest.md`, and fires a
   macOS notification if there are new matches.

5. Report the digest's headline counts and any 🆕 items. Do not bid, register,
   submit, or download anything — this is read-only reconnaissance.
