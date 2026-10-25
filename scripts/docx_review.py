#!/usr/bin/env python3
"""Review and fix the formatting of Word (.docx) files before they leave the building.

Three commands:

    docx_review.py check  FILE...  [--page a4|letter|any] [--format text|json] [--gate]
    docx_review.py render FILE     --out DIR
    docx_review.py fix    FILE -o OUT [--a4] [--footer TEXT] [--font NAME] [--borders]
                          [--repeat-headers] [--link-urls] [--drop-empty-shapes]
                          [--author NAME] [--title TEXT] [--all]

`check` is report-only unless `--gate` (docs/gate-rollout.md). It reads the XML, so it sees what a
reader sees only where XML and layout agree; `render` produces the PDF and page images the
reviewer must actually look at. A file that could not be read is `error`, never a pass.

`fix` never changes wording. It writes a new file, then compares the plain text of input and
output and fails if any word differs, so a fix that touches copy is refused rather than shipped.

Born from a tender lodgement (2026-10-25) where every defect below was present at once: a form
converted from PDF left nine empty answer boxes (one in a footer, printed on every page), pandoc
output had no page size (US Letter) and no page numbers, inserted tables had no borders, and the
body font was Aptos, which older Word substitutes.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import docx  # python-docx (PyPI `docx` is a different, abandoned package)
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt
except ImportError:  # pragma: no cover - reported as an error result, not a traceback
    docx = None

A4 = (210.0, 297.0)
LETTER = (215.9, 279.4)
# Fonts that are absent from Office 2019 and earlier, or from non-Windows readers, and get
# substituted. Aptos is Microsoft 365's 2023+ default; pandoc's reference.docx uses it.
FRAGILE_FONTS = {"Aptos", "Aptos Display", "Aptos Narrow", "Aptos Serif"}
URL = re.compile(r"https?://[^\s<>()\"]+[^\s<>()\".,;:]")
TYPED_BULLET = re.compile(r"^\s*([•·▪◦‣\-\*])\s+\S")
TYPED_NUMBER = re.compile(r"^\s*\d{1,2}[.)]\s+\S")
PLACEHOLDER = re.compile(
    r"\{\{[^}]*\}\}|\bTBD\b|\bTODO\b|\bXXX\b|\[insert[^\]]*\]|lorem ipsum", re.I
)
MARKDOWN = re.compile(r"\*\*[^*]+\*\*|^#{1,6} |`[^`]+`|\[[^\]]+\]\([^)]+\)")
DASHES = re.compile(r"[—–]")

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Finding:
    rule: str
    severity: str  # high | medium | low
    where: str
    detail: str
    fix: str = ""  # the `fix` flag that addresses it, if any


@dataclass
class Report:
    file: str
    status: str = "pass"  # pass | findings | error
    error: str = ""
    checks_run: int = 0
    properties: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    def add(self, *args, **kw) -> None:
        self.findings.append(Finding(*args, **kw))


# --------------------------------------------------------------------------------------- check


def _mm(v) -> float | None:
    return None if v is None else round(v.mm, 1)


def _theme_fonts(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if re.fullmatch(r"word/theme/theme\d*\.xml", n)]
        out: set[str] = set()
        for n in names:
            xml = z.read(n).decode("utf-8", "replace")
            out |= set(re.findall(r'<a:(?:major|minor)Font>\s*<a:latin typeface="([^"]*)"', xml))
        return out


def _style_fonts(d) -> set[str]:
    xml = d.styles.element.xml
    return set(re.findall(r'w:ascii="([^"]+)"', xml))


def _table_has_borders(t) -> bool:
    tblPr = t._tbl.tblPr
    if tblPr is not None:
        b = tblPr.find(qn("w:tblBorders"))
        if b is not None and any(c.get(qn("w:val")) not in (None, "nil", "none") for c in b):
            return True
    if "w:tcBorders" in t._tbl.xml:
        return True
    style = t.style
    while style is not None:
        sx = style.element.xml
        if re.search(r'<w:(insideH|insideV|top|left)\b[^>]*w:val="(?!nil|none)', sx):
            return True
        style = style.base_style
    return False


def _empty_shapes(root) -> int:
    """Drawn shapes (text boxes, groups) whose text content is empty."""
    n = 0
    for drawing in root.iter(qn("w:drawing")):
        if drawing.find(".//" + qn("pic:pic")) is not None:  # pictures are content
            continue
        if not "".join(t.text or "" for t in drawing.iter(qn("w:t"))).strip():
            n += 1
    return n


def check(path: Path, page: str = "a4", owner: str | None = None) -> Report:
    rep = Report(file=str(path))
    if docx is None:
        rep.status, rep.error = (
            "error",
            "python-docx is not installed (see: check_deps.py --for docx-review)",
        )
        return rep
    try:
        d = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001 - any unreadable file is an error result
        rep.status, rep.error = "error", f"{type(exc).__name__}: {exc}"
        return rep

    def ran() -> None:
        rep.checks_run += 1

    # 1. Page size and orientation, per section.
    ran()
    for i, s in enumerate(d.sections, 1):
        w, h = _mm(s.page_width), _mm(s.page_height)
        if w is None or h is None:
            rep.add(
                "page-size-missing",
                "high",
                f"section {i}",
                "No page size is set; Word falls back to US Letter.",
                "--a4",
            )
            continue
        size = tuple(sorted((w, h)))
        if page == "a4" and max(abs(size[0] - A4[0]), abs(size[1] - A4[1])) > 2:
            rep.add("page-size", "high", f"section {i}", f"Page is {w} x {h} mm, not A4.", "--a4")
        if page == "letter" and max(abs(size[0] - LETTER[0]), abs(size[1] - LETTER[1])) > 2:
            rep.add("page-size", "high", f"section {i}", f"Page is {w} x {h} mm, not US Letter.")

    # 2. Page numbers in some header or footer.
    ran()
    hf_xml = ""
    for s in d.sections:
        for part in (s.header, s.footer, s.first_page_header, s.first_page_footer):
            try:
                hf_xml += part._element.xml
            except Exception:  # noqa: BLE001 - absent parts
                pass
    if not re.search(r"\b(PAGE|NUMPAGES)\b", hf_xml):
        rep.add(
            "no-page-numbers",
            "medium",
            "headers and footers",
            "No PAGE field in any header or footer, so pages are unnumbered.",
            "--footer",
        )

    # 3. Fonts that will be substituted.
    ran()
    fragile = (_theme_fonts(path) | _style_fonts(d)) & FRAGILE_FONTS
    if fragile:
        rep.add(
            "fragile-font",
            "medium",
            "theme and styles",
            f"Uses {', '.join(sorted(fragile))}; Office 2019 and older substitute it.",
            "--font",
        )

    # 4. Empty drawn shapes: leftover form fields and text boxes from PDF conversion.
    ran()
    body_empty = _empty_shapes(d.element.body)
    if body_empty:
        rep.add(
            "empty-shapes",
            "high",
            "body",
            f"{body_empty} drawn shape(s) with no text, e.g. answer boxes left empty "
            "after answers were typed outside them.",
            "--drop-empty-shapes",
        )
    for i, s in enumerate(d.sections, 1):
        for name in ("header", "footer"):
            part = getattr(s, name)
            if part.is_linked_to_previous:
                continue
            width = s.page_width or Mm(A4[0])
            boxes = [
                e
                for e in part._element.iter(qn("wp:extent"))
                if int(e.get("cx", 0)) > 0.6 * width and int(e.get("cy", 0)) > Mm(5)
            ]
            if boxes:
                rep.add(
                    "repeating-shape",
                    "high",
                    f"section {i} {name}",
                    f"{len(boxes)} large empty shape(s) in the {name}, printed on every page of "
                    "the section and liable to overlap body text.",
                    "--drop-empty-shapes",
                )

    # 5. Tables: borders, consistency, repeating header rows.
    ran()
    tables = d.tables
    ruled = [t for t in tables if _table_has_borders(t)]
    for i, t in enumerate(tables, 1):
        if not _table_has_borders(t) and len(t.rows) > 1:
            sev = "medium" if ruled else "low"
            note = " while other tables in the file are ruled" if ruled else ""
            rep.add(
                "table-borders",
                sev,
                f"table {i}",
                f"{len(t.rows)}-row table has no borders{note}.",
                "--borders",
            )
        if len(t.rows) >= 8 and "w:tblHeader" not in t._tbl.xml:
            rep.add(
                "table-header-repeat",
                "low",
                f"table {i}",
                f"{len(t.rows)}-row table does not repeat its header row across pages.",
                "--repeat-headers",
            )

    # 6-10. Paragraph-level text checks.
    for _ in range(5):
        ran()
    typed_bullets = typed_numbers = blank_runs = 0
    bare_urls: list[str] = []
    prev_blank = 0
    for i, p in enumerate(d.paragraphs):
        t = p.text
        has_num = p._p.pPr is not None and p._p.pPr.find(qn("w:numPr")) is not None
        if TYPED_BULLET.match(t) and not has_num:
            typed_bullets += 1
        if TYPED_NUMBER.match(t) and not has_num and p.style.name.lower().startswith("list"):
            typed_numbers += 1
        if (
            not t.strip()
            and p._p.find(".//" + qn("w:drawing")) is None
            and "w:sectPr" not in p._p.xml
            and "w:br" not in p._p.xml
        ):
            prev_blank += 1
            if prev_blank == 2:
                blank_runs += 1
        else:
            prev_blank = 0
        linked = "".join(
            x.text or "" for h in p._p.iter(qn("w:hyperlink")) for x in h.iter(qn("w:t"))
        )
        bare_urls += [u for u in URL.findall(t) if u not in linked]
        for rule, rx, sev, what in (
            ("placeholder", PLACEHOLDER, "high", "placeholder text"),
            ("markdown-residue", MARKDOWN, "medium", "markdown syntax"),
            ("dash", DASHES, "low", "an em or en dash"),
        ):
            m = rx.search(t)
            if m:
                rep.add(
                    rule,
                    sev,
                    f"paragraph {i + 1}",
                    f"Contains {what}: “{t[max(0, m.start() - 30) : m.end() + 30].strip()}”",
                )
        if "  " in t.strip():
            rep.add(
                "double-space", "low", f"paragraph {i + 1}", f"Double space in “{t.strip()[:60]}”"
            )
    for t in tables:
        for row in t.rows:
            for c in row.cells:
                linked = "".join(
                    x.text or "" for h in c._tc.iter(qn("w:hyperlink")) for x in h.iter(qn("w:t"))
                )
                bare_urls += [u for u in URL.findall(c.text) if u not in linked]
                m = PLACEHOLDER.search(c.text)
                if m:
                    rep.add(
                        "placeholder",
                        "high",
                        "table cell",
                        f"Contains placeholder text: “{c.text[:60]}”",
                    )
    if typed_bullets:
        rep.add(
            "typed-bullets",
            "low",
            "body",
            f"{typed_bullets} paragraph(s) start with a typed bullet character instead of a Word list.",
        )
    if typed_numbers:
        rep.add(
            "typed-numbering",
            "low",
            "body",
            f"{typed_numbers} list paragraph(s) carry typed numbers instead of Word numbering.",
        )
    if blank_runs:
        rep.add(
            "blank-paragraphs",
            "low",
            "body",
            f"{blank_runs} run(s) of two or more empty paragraphs used as spacing.",
        )
    if bare_urls:
        uniq = sorted(set(bare_urls))
        rep.add(
            "bare-urls",
            "low",
            "body",
            f"{len(uniq)} URL(s) are plain text, not clickable: {', '.join(uniq[:3])}",
            "--link-urls",
        )

    # 11. Document properties carried over from someone else's template.
    ran()
    cp = d.core_properties
    rep.properties = {
        k: getattr(cp, k) or "" for k in ("author", "last_modified_by", "title", "subject")
    }
    if owner:
        foreign = {
            k: v
            for k, v in rep.properties.items()
            if k in ("author", "last_modified_by") and v and v != owner
        }
        if foreign:
            rep.add(
                "foreign-properties",
                "medium",
                "document properties",
                "Properties name someone other than "
                + repr(owner)
                + ": "
                + "; ".join(f"{k}={v!r}" for k, v in foreign.items()),
                "--author",
            )
    if not rep.properties["title"]:
        rep.add("no-title", "low", "document properties", "No document title is set.", "--title")

    # 12. Comments and tracked changes left in.
    ran()
    with zipfile.ZipFile(path) as z:
        # pandoc always writes an empty comments part; only real <w:comment> elements count.
        has_comments = any(
            b"<w:comment " in z.read(n)
            for n in z.namelist()
            if re.fullmatch(r"word/comments\w*\.xml", n)
        )
    body_xml = d.element.body.xml
    if has_comments or "<w:ins " in body_xml or "<w:del " in body_xml:
        rep.add(
            "review-residue",
            "high",
            "document",
            "Comments or tracked changes are still in the file.",
        )

    rep.findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.rule))
    rep.status = "findings" if rep.findings else "pass"
    return rep


# -------------------------------------------------------------------------------------- render


def _renderer() -> str | None:
    if shutil.which("soffice") or shutil.which("libreoffice"):
        return "libreoffice"
    if sys.platform == "darwin" and Path("/Applications/Pages.app").exists():
        return "pages"
    return None


def render(path: Path, out: Path) -> dict:
    """Render to PDF and per-page PNGs. Returns a result dict; status `unverified` if no renderer."""
    out.mkdir(parents=True, exist_ok=True)
    which = _renderer()
    result = {
        "file": str(path),
        "renderer": which,
        "pdf": None,
        "pages": [],
        "status": "unverified",
    }
    if which is None:
        result["error"] = "no renderer: install LibreOffice (soffice) or, on macOS, Pages"
        return result
    pdf = out / (path.stem + ".pdf")
    # Render a copy so the original is never opened, locked or autosaved by the renderer.
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / path.name
        shutil.copy2(path, src)
        if which == "libreoffice":
            exe = shutil.which("soffice") or shutil.which("libreoffice")
            subprocess.run(
                [exe, "--headless", "--convert-to", "pdf", "--outdir", str(out), str(src)],
                check=False,
                capture_output=True,
                timeout=300,
            )
        else:
            script = (
                f'with timeout of 300 seconds\ntell application "Pages"\n'
                f'set d to open POSIX file "{src}"\ndelay 3\n'
                f'export d to POSIX file "{pdf}" as PDF\nclose d saving no\nend tell\nend timeout'
            )
            subprocess.run(
                ["osascript", "-e", script], check=False, capture_output=True, timeout=360
            )
    if not pdf.exists():
        result["error"] = f"{which} produced no PDF"
        return result
    result["pdf"] = str(pdf)
    blob = pdf.read_bytes()
    result["page_count"] = len(re.findall(rb"/Type\s*/Page[^s]", blob))
    boxes = sorted(set(re.findall(rb"/MediaBox\s*\[\s*([\d.\s]+)\]", blob)))
    result["media_boxes_pt"] = [b.decode().split() for b in boxes]
    if shutil.which("pdftoppm"):
        subprocess.run(
            ["pdftoppm", "-r", "70", "-png", str(pdf), str(out / path.stem)],
            check=False,
            capture_output=True,
            timeout=300,
        )
        result["pages"] = sorted(str(p) for p in out.glob(f"{path.stem}-*.png"))
    result["status"] = "rendered"
    if which == "pages":
        result["note"] = (
            "Pages approximates Word: anchored shapes and table widths can differ. "
            "Treat overlaps as real until the XML says otherwise."
        )
    return result


# ----------------------------------------------------------------------------------------- fix


def _plain_words(path: Path) -> list[str]:
    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts += [c.text for c in row.cells]
    text = " ".join(parts)
    text = re.sub(r"^[•·▪◦‣]\s*", "", text)
    return [w for w in re.split(r"[\s•·▪◦‣]+", text) if w]


def _hyperlink_runs(par) -> None:
    """Turn bare URLs inside plain runs into hyperlinks, keeping run formatting."""
    for run in list(par.runs):
        text = run.text
        if not URL.search(text) or run._r.getparent().tag == qn("w:hyperlink"):
            continue
        pieces, pos = [], 0
        for m in URL.finditer(text):
            pieces += [("t", text[pos : m.start()]), ("u", m.group(0))]
            pos = m.end()
        pieces.append(("t", text[pos:]))
        anchor = run._r
        for kind, chunk in pieces:
            if not chunk:
                continue
            new_r = OxmlElement("w:r")
            if run._r.rPr is not None:
                import copy

                new_r.append(copy.deepcopy(run._r.rPr))
            t = OxmlElement("w:t")
            t.text = chunk
            t.set(qn("xml:space"), "preserve")
            new_r.append(t)
            if kind == "u":
                rid = par.part.relate_to(chunk, RT.HYPERLINK, is_external=True)
                link = OxmlElement("w:hyperlink")
                link.set(qn("r:id"), rid)
                rpr = new_r.find(qn("w:rPr"))
                if rpr is None:
                    rpr = OxmlElement("w:rPr")
                    new_r.insert(0, rpr)
                for tag, val in (("w:color", "0563C1"), ("w:u", "single")):
                    el = OxmlElement(tag)
                    el.set(qn("w:val"), val)
                    rpr.append(el)
                link.append(new_r)
                anchor.addnext(link)
                anchor = link
            else:
                anchor.addnext(new_r)
                anchor = new_r
        run._r.getparent().remove(run._r)


def _field(par, instr: str) -> None:
    for kind, text in (
        ("begin", None),
        ("instr", instr),
        ("separate", None),
        ("text", "1"),
        ("end", None),
    ):
        r = OxmlElement("w:r")
        if kind in ("begin", "separate", "end"):
            fc = OxmlElement("w:fldChar")
            fc.set(qn("w:fldCharType"), kind)
            r.append(fc)
        elif kind == "instr":
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = f" {text} "
            r.append(it)
        else:
            t = OxmlElement("w:t")
            t.text = text
            r.append(t)
        par._p.append(r)


def fix(
    path: Path,
    out: Path,
    *,
    a4=False,
    footer=None,
    font=None,
    borders=False,
    repeat_headers=False,
    link_urls=False,
    drop_empty_shapes=False,
    author=None,
    title=None,
) -> dict:
    if out.resolve() == path.resolve():
        raise SystemExit("fix writes a new file; pass an -o different from the input")
    d = docx.Document(str(path))
    done: list[str] = []
    if a4:
        for s in d.sections:
            s.page_width, s.page_height = Mm(210), Mm(297)
            for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
                if getattr(s, side) is None:
                    setattr(s, side, Mm(20))
        done.append("a4")
    if font:
        for part in d.part.package.iter_parts():
            if re.search(r"/word/theme/theme\d*\.xml$", str(part.partname)):
                blob = part.blob
                for f in sorted(FRAGILE_FONTS, key=len, reverse=True):
                    blob = blob.replace(f'typeface="{f}"'.encode(), f'typeface="{font}"'.encode())
                part._blob = blob
        d.styles["Normal"].font.name = font
        done.append(f"font={font}")
    if drop_empty_shapes:
        n = 0
        roots = [d.element.body] + [
            getattr(s, k)._element
            for s in d.sections
            for k in ("header", "footer")
            if not getattr(s, k).is_linked_to_previous
        ]
        for root in roots:
            for drawing in list(root.iter(qn("w:drawing"))):
                if drawing.find(".//" + qn("pic:pic")) is not None:
                    continue
                if "".join(t.text or "" for t in drawing.iter(qn("w:t"))).strip():
                    continue
                ext = drawing.find(".//" + qn("wp:extent"))
                if root is not d.element.body and (ext is None or int(ext.get("cx", 0)) < Mm(100)):
                    continue  # small header/footer shapes are logos and rules; leave them
                run = next((a for a in drawing.iterancestors() if a.tag == qn("w:r")), None)
                if run is None:
                    continue
                par = run.getparent()
                par.remove(run)
                n += 1
                if (
                    par.tag == qn("w:p")
                    and not "".join(par.itertext()).strip()
                    and par.find(".//" + qn("w:sectPr")) is None
                    and root is d.element.body
                    and par.find(".//" + qn("w:drawing")) is None
                ):
                    par.getparent().remove(par)
        done.append(f"dropped {n} empty shape(s)")
    if borders:
        n = 0
        for t in d.tables:
            if _table_has_borders(t):
                continue
            tblPr = t._tbl.tblPr
            el = OxmlElement("w:tblBorders")
            for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
                b = OxmlElement(f"w:{side}")
                for k, v in (("val", "single"), ("sz", "4"), ("space", "0"), ("color", "808080")):
                    b.set(qn(f"w:{k}"), v)
                el.append(b)
            tblPr.append(el)
            n += 1
        done.append(f"ruled {n} table(s)")
    if repeat_headers:
        for t in d.tables:
            if len(t.rows) > 1:
                trPr = t.rows[0]._tr.get_or_add_trPr()
                if trPr.find(qn("w:tblHeader")) is None:
                    trPr.append(OxmlElement("w:tblHeader"))
        done.append("header rows repeat")
    if link_urls:
        pars = list(d.paragraphs) + [
            p for t in d.tables for r in t.rows for c in r.cells for p in c.paragraphs
        ]
        for p in pars:
            _hyperlink_runs(p)
        done.append("linked URLs")
    if footer is not None:
        for s in d.sections:
            if "PAGE" in s.footer._element.xml:
                continue
            s.footer.is_linked_to_previous = False
            p = s.footer.add_paragraph()
            r = p.add_run(f"{footer}    Page " if footer else "Page ")
            r.font.size = Pt(9)
            _field(p, "PAGE")
            p.add_run(" of ").font.size = Pt(9)
            _field(p, "NUMPAGES")
        done.append("page-number footer")
    if author is not None:
        d.core_properties.author = author
        d.core_properties.last_modified_by = author
        done.append("author")
    if title is not None:
        d.core_properties.title = title
        done.append("title")
    d.save(str(out))
    before, after = _plain_words(path), _plain_words(out)
    if before != after:
        out.unlink()
        raise SystemExit("refused: the fix changed the document's words; nothing was written")
    return {"file": str(path), "output": str(out), "applied": done, "words_unchanged": True}


# ----------------------------------------------------------------------------------------- cli


def _print_text(rep: Report) -> None:
    print(
        f"== {rep.file}: {rep.status.upper()} ({rep.checks_run} checks, {len(rep.findings)} findings)"
    )
    if rep.properties:
        print("   properties: " + "; ".join(f"{k}={v!r}" for k, v in rep.properties.items() if v))
    if rep.error:
        print(f"   error: {rep.error}")
    for f in rep.findings:
        hint = f"  [fix: {f.fix}]" if f.fix else ""
        print(f"   {f.severity:<6} {f.rule:<20} {f.where}: {f.detail}{hint}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("files", nargs="+", type=Path)
    c.add_argument("--page", choices=("a4", "letter", "any"), default="a4")
    c.add_argument("--format", choices=("text", "json"), default="text")
    c.add_argument("--owner", help="flag author/last-modified properties that name anyone else")
    c.add_argument(
        "--gate", action="store_true", help="exit 1 on any high-severity finding or error"
    )
    r = sub.add_parser("render")
    r.add_argument("file", type=Path)
    r.add_argument("--out", type=Path, required=True)
    f = sub.add_parser("fix")
    f.add_argument("file", type=Path)
    f.add_argument("-o", "--output", type=Path, required=True)
    f.add_argument("--a4", action="store_true")
    f.add_argument(
        "--footer", metavar="TEXT", help="add a footer: TEXT then Page X of Y ('' for numbers only)"
    )
    f.add_argument("--font", metavar="NAME", help="replace fragile theme fonts (Aptos) with NAME")
    f.add_argument("--borders", action="store_true")
    f.add_argument("--repeat-headers", action="store_true")
    f.add_argument("--link-urls", action="store_true")
    f.add_argument("--drop-empty-shapes", action="store_true")
    f.add_argument("--author")
    f.add_argument("--title")
    f.add_argument(
        "--all",
        action="store_true",
        help="a4, borders, repeat-headers, link-urls, drop-empty-shapes, font Calibri, numbered footer",
    )
    a = ap.parse_args(argv)

    if a.cmd == "check":
        reps = [check(p, a.page, a.owner) for p in a.files]
        if a.format == "json":
            print(json.dumps([asdict(x) for x in reps], indent=2))
        else:
            for x in reps:
                _print_text(x)
            print(f"-- {len(reps)} file(s): " + ", ".join(f"{x.status}" for x in reps))
        if a.gate:
            bad = any(
                x.status == "error" or any(f.severity == "high" for f in x.findings) for x in reps
            )
            return 1 if bad else 0
        return 2 if any(x.status == "error" for x in reps) else 0
    if a.cmd == "render":
        res = render(a.file, a.out)
        print(json.dumps(res, indent=2))
        return 0 if res["status"] == "rendered" else 2
    if docx is None:
        print(
            "python-docx is not installed (see: check_deps.py --for docx-review)", file=sys.stderr
        )
        return 2
    if a.all:
        a.a4 = a.borders = a.repeat_headers = a.link_urls = a.drop_empty_shapes = True
        a.font = a.font or "Calibri"
        a.footer = "" if a.footer is None else a.footer
    res = fix(
        a.file,
        a.output,
        a4=a.a4,
        footer=a.footer,
        font=a.font,
        borders=a.borders,
        repeat_headers=a.repeat_headers,
        link_urls=a.link_urls,
        drop_empty_shapes=a.drop_empty_shapes,
        author=a.author,
        title=a.title,
    )
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
