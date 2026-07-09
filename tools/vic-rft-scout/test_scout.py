"""Deterministic tests for the scout core (parse / match / diff / digest).

No network — uses fixtures/sample_listings.json (real listing data captured
from tenders.vic.gov.au). Run:  python -m unittest -v
"""
import json
import unittest
from pathlib import Path

import scout

HERE = Path(__file__).resolve().parent
CFG = scout.load_config()
RECORDS = json.loads((HERE / "fixtures" / "sample_listings.json").read_text())
TENDERS = {t.rfx: t for t in scout.tenders_from_records(RECORDS)}


class WordMatch(unittest.TestCase):
    def test_ict_not_in_victoria(self):
        # The bug that shipped in v1: 'ict' fired on 'Victoria'.
        self.assertFalse(scout._word_match("ict", "victorian fisheries authority"))
        self.assertFalse(scout._word_match("ict", "triple zero victoria"))

    def test_ict_matches_standalone(self):
        self.assertTrue(scout._word_match("ict", "vicroads ict procurement"))

    def test_ai_boundaries(self):
        self.assertTrue(scout._word_match("ai", "ai model for demand"))
        self.assertFalse(scout._word_match("ai", "contains detail and maintenance"))

    def test_web_not_website(self):
        self.assertTrue(scout._word_match("web", "new web portal"))
        self.assertFalse(scout._word_match("web", "corporate website refresh"))


class Parsing(unittest.TestCase):
    def test_row0(self):
        rfx, status, ttype = scout._parse_row0("REQ-33834\nOpen\nRequest for Tender")
        self.assertEqual(rfx, "REQ-33834")
        self.assertEqual(status, "Open")
        self.assertEqual(ttype, "Request for Tender")

    def test_details(self):
        buyer, unspsc = scout._parse_details(
            "Governance, Risk & Compliance Software\n"
            "Issued by: Westernport Water\n"
            "UNSPSC 1: 43232305 - Data base reporting software - 50%\n"
            "UNSPSC 2: 43230000 - Software - 50%")
        self.assertEqual(buyer, "Westernport Water")
        self.assertEqual(unspsc, ["43232305 - Data base reporting software", "43230000 - Software"])

    def test_date_cell(self):
        opened, closing, iso = scout._parse_date_cell(
            "Opened\nFri, 05 June 2026 12:00 pm\nClosing\nMon, 31 August 2026 2:00 pm")
        self.assertEqual(closing, "Mon, 31 August 2026 2:00 pm")
        self.assertEqual(iso, "2026-08-31")

    def test_records_get_iso(self):
        self.assertEqual(TENDERS["2025018"].closing_iso, "2026-08-31")


class Matching(unittest.TestCase):
    def _match(self, rfx):
        return scout.match_reasons(TENDERS[rfx], CFG)

    def test_expected_matches(self):
        for rfx in ["REQ-33834", "EOI-PINP", "N1000271", "25-26-122",
                    "N1000253", "2025018", "RITM0044432", "C5379-2026"]:
            self.assertTrue(self._match(rfx), f"{rfx} should match")

    def test_expected_non_matches(self):
        # The 'Victoria' traps + unrelated construction/health must NOT match.
        for rfx in ["BBVNPRP26", "2026-009", "DOT44674", "EGH-Willaura-2026", "ATN 2841"]:
            self.assertFalse(self._match(rfx), f"{rfx} should NOT match (got {self._match(rfx)})")

    def test_match_reasons_are_specific(self):
        self.assertIn("UNSPSC 84131609", self._match("25-26-122"))
        r = self._match("2025018")
        self.assertTrue(any("UNSPSC 43" in x for x in r))
        self.assertTrue(any("software" in x for x in r))


class DiffAndDigest(unittest.TestCase):
    def setUp(self):
        self.matched = [(t, scout.match_reasons(t, CFG))
                        for t in TENDERS.values() if scout.match_reasons(t, CFG)]

    def test_new_ids_diff(self):
        seen = {"326699": {}, "324729": {}}  # two already seen
        new_ids = {t.id for t, _ in self.matched if t.id not in seen}
        self.assertNotIn("326699", new_ids)
        self.assertIn("323293", new_ids)  # GRC advance notice is new

    def test_digest_structure(self):
        new_ids = {t.id for t, _ in self.matched}
        digest = scout.build_digest(self.matched, new_ids, "2026-07-09")
        self.assertIn("# VIC Gov RFT scout — 2026-07-09", digest)
        self.assertIn("Governance, Risk & Compliance Software", digest)  # future section
        self.assertIn("view?id=323293", digest)                          # direct link
        self.assertNotIn("Newhaven Pier", digest)                        # filtered out
        self.assertIn("🆕", digest)                                       # new-flag rendered
        # open section header present with a count
        self.assertIn("🎯 Open now", digest)
        self.assertIn("🔭 Advance notices", digest)


if __name__ == "__main__":
    unittest.main()
