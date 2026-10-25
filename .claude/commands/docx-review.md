# Docx Review — Word Formatting Review and Fix

Checks a Word (.docx) file's formatting before it goes to a reader outside the company: a tender, a submission, a contract, a report. It reads the XML for defects a reader will see, renders the file to PDF so the pages are actually looked at, and, when asked, fixes them without changing a word.

The skill exists because XML and layout disagree. A form converted from PDF can carry empty answer boxes that only show once rendered, and a footer shape prints on every page of its section. The check finds candidates; the render is the evidence.

## When to use

- Before lodging or sending any .docx: tenders, RFIs, submissions, proposals, contracts
- After filling someone else's template (forms converted from PDF are the worst case)
- After generating a .docx with pandoc or python-docx, whose defaults leave out page size, page numbers and table rules
- When asked to "check the formatting" or "fix the formatting" of a Word file

## Usage

```
/docx-review <file.docx> [<file.docx> ...]          # review only
/docx-review --fix <file.docx> [...]               # review, then fix
/docx-review --owner "Company Pty Ltd" <file.docx>  # also flag properties naming anyone else
```

## Autonomy

Review runs to completion without pausing and ends with a report. A request to fix is the authority to fix. Never overwrite the input: write a new file, or fix the source that generates it.

---

## Workflow

### Step 1: Resolve paths

```bash
CW_HOME="${CHIEF_WIGGUM_HOME:-$HOME/repos/chief-wiggum}"
CW_HOME=$(python3 "$CW_HOME/scripts/env.py" home)
# Pin the interpreter CW scripts run under (chief-wiggum#374).
CW_PY=$(python3 "$CW_HOME/scripts/env.py" python) || CW_PY=python3
CW_TMP=$("${CW_PY:-python3}" "$CW_HOME/scripts/env.py" tmp)
"${CW_PY:-python3}" "$CW_HOME/scripts/check_deps.py" --for docx-review
```

The profile needs python-docx. A renderer (LibreOffice, or Pages on macOS) and `pdftoppm` are needed for Step 4. If one is missing, install it: never punt to the user.

### Step 2: Find what generated the file

Before reviewing, find out whether the file is built from a source: a markdown file run through pandoc, a fill script over a template, an export. Search the repo that owns it for the filename, `pandoc`, `python-docx` or `docx.Document`, and compare hashes with the committed copy.

- **Generated:** every fix goes into the generator, then the file is rebuilt. A hand fix to generated output is lost on the next build.
- **Hand-made:** fixes go into a new file next to it, via Step 6.

### Step 3: Check

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/docx_review.py" check --owner "$OWNER" FILE...
# --page letter for US documents; --format json for machine use; --gate exits 1 on any high finding
```

Statuses are `pass`, `findings` or `error`. An `error` is a file that could not be read: report it as unchecked, never as clean. Every report prints how many checks ran.

Rules, by severity:

| Severity | Rule | What it means |
|---|---|---|
| high | `page-size-missing`, `page-size` | No page size (Word uses US Letter) or not the expected size |
| high | `empty-shapes`, `repeating-shape` | Empty drawn boxes in the body, or a large one in a header or footer that prints on every page |
| high | `placeholder` | `{{...}}`, TBD, TODO, `[insert ...]` left in |
| high | `review-residue` | Comments or tracked changes still in the file |
| medium | `no-page-numbers` | No PAGE field in any header or footer |
| medium | `fragile-font` | Aptos, which Office 2019 and older substitute |
| medium | `table-borders` | An unruled table when others in the file are ruled |
| medium | `foreign-properties` | Author or last-modified names someone other than `--owner` |
| medium | `markdown-residue` | `**bold**`, `# heading` or backticks in body text |
| low | `table-header-repeat`, `bare-urls`, `typed-bullets`, `typed-numbering`, `blank-paragraphs`, `double-space`, `dash`, `no-title` | Polish |

`dash` matters for outward copy under the house voice rules (no em dashes). It is low because some documents legitimately use them.

### Step 4: Render and look

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/docx_review.py" render FILE --out "$CW_TMP/docx-review"
```

This renders a copy, so the original is never opened or locked. It returns the PDF, the page count, the page sizes in points (A4 is 595 x 842) and one PNG per page. Build a contact sheet and look at every page:

```bash
magick montage "$CW_TMP/docx-review/<stem>"-*.png -tile 4x -geometry +4+4 -background '#888' "$CW_TMP/docx-review/sheet.png"
```

Look for what the XML check cannot see: shapes overlapping text, tables running off the page, orphaned headings, a near-empty last page, and the first page's impression. Status `unverified` means no renderer ran. Say so; it is not a pass. Pages approximates Word, so confirm an overlap in the XML before blaming Word.

### Step 5: Report

Lead with what a reader would notice, high severity first, one bullet per finding with where it is and what fixes it. Separate findings that come from someone else's template, such as the buyer's footer or their page numbering, from ones we introduced; leave the template's own conventions alone unless they break the document.

If only a review was asked for, stop here.

### Step 6: Fix

**Generated file:** fix the generator and rebuild. Keep the fixes general (page setup, footer, fonts, borders, links, removing empty template shapes) rather than patching one document's output.

**Hand-made file:**

```bash
"${CW_PY:-python3}" "$CW_HOME/scripts/docx_review.py" fix FILE -o FIXED.docx --all --author "$OWNER" --title "$TITLE"
# or pick: --a4 --footer "TEXT" --font Calibri --borders --repeat-headers --link-urls --drop-empty-shapes
```

`fix` compares the plain text of input and output and refuses, writing nothing, if any word changed. `--drop-empty-shapes` removes empty drawn shapes in the body and large ones in headers and footers, and keeps pictures and small header or footer shapes such as logos and rules.

Branding (a logo above the title, a company footer) is a fix only for our own documents. Leave a buyer's form carrying the buyer's branding.

### Step 7: Verify

1. Re-run Step 3 on the output. Every high finding must be gone, or explained.
2. Re-run Step 4 and look at every page again.
3. Diff the words against the original. `fix` does this itself; for a rebuilt generator, compare `pandoc -t plain` of the old and new files.
4. Replace the delivered copy only after the checks pass, and only where the original is recoverable (committed in git, or kept beside it).

Report what changed, the before and after page counts and sizes, and any finding left as is, with the reason.
