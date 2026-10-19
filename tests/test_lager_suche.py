import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lager_suche as ls  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "inventar_beispiel.csv"


def fixture_bytes():
    return FIXTURE.read_bytes()


def semicolon_cp1252_bytes():
    text = fixture_bytes().decode("utf-8")
    return text.replace('","', '";"').encode("cp1252")


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.config = ls.load_config(None)

    def test_reads_plain_utf8(self):
        items = ls.parse_inventory(fixture_bytes(), self.config)
        self.assertEqual(len(items), 16)
        self.assertEqual(items[0].name, "Galaxy Quest")
        self.assertEqual(items[0].description, "DVD")
        self.assertEqual(items[0].location, "Keller » Kiste 3")

    def test_reads_utf8_bom(self):
        items = ls.parse_inventory(b"\xef\xbb\xbf" + fixture_bytes(), self.config)
        self.assertEqual(len(items), 16)
        self.assertEqual(items[0].name, "Galaxy Quest")

    def test_reads_semicolon_cp1252(self):
        items = ls.parse_inventory(semicolon_cp1252_bytes(), self.config)
        self.assertEqual(len(items), 16)
        self.assertEqual(items[9].name, "Über Menschen")

    def test_missing_required_column_is_rejected(self):
        with self.assertRaises(ls.InventoryError):
            ls.parse_inventory(b'"Titel","Wo"\n"x","y"\n', self.config)

    def test_empty_file_is_rejected(self):
        with self.assertRaises(ls.InventoryError):
            ls.parse_inventory(b"", self.config)

    def test_header_only_is_rejected(self):
        header = fixture_bytes().split(b"\n", 1)[0] + b"\n"
        with self.assertRaises(ls.InventoryError):
            ls.parse_inventory(header, self.config)

    def test_column_mapping_is_configurable(self):
        config = ls.load_config(None)
        config["columns"]["name"] = ["Titel"]
        config["columns"]["location"] = ["Wo"]
        items = ls.parse_inventory('"Titel";"Wo"\n"Alien";"Speicher » Kiste 9"\n'.encode(), config)
        self.assertEqual(items[0].name, "Alien")
        self.assertEqual(items[0].container, "Kiste 9")

    def test_location_parts(self):
        items = ls.parse_inventory(fixture_bytes(), self.config)
        sherlock = next(i for i in items if i.name.startswith("Sherlock"))
        self.assertEqual(sherlock.place, "Wohnzimmer")
        self.assertEqual(sherlock.container, "Kallax Regal Weiß » 1x1")


class NormalizeTests(unittest.TestCase):
    def test_umlauts_case_punctuation(self):
        self.assertEqual(ls.normalize("Zurück in die ZUKUNFT!"), "zurueck in die zukunft")
        self.assertEqual(ls.normalize("Ocean's Eleven"), "oceans eleven")
        self.assertEqual(ls.normalize("Straße – 12V/10A"), "strasse 12v 10a")

    def test_umlaut_spellings_match(self):
        self.assertEqual(ls.normalize("Über"), ls.normalize("ueber"))


class ParseRequestTests(unittest.TestCase):
    def test_prefixes(self):
        cases = {
            "wo ist galaxy quest": ("search", "galaxy quest"),
            "Wo sind die Netzteile?": ("search", "netzteile"),
            "wo liegt der Hobbit": ("search", "hobbit"),
            "lager tolkien": ("search", "tolkien"),
            "Kiste 4": ("box", "4"),
            "kiste küchenkram": ("box", "küchenkram"),
            "kisten": ("overview", ""),
            "lager": ("overview", ""),
            "galaxy": ("search", "galaxy"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(ls.parse_request(text), expected)

    def test_article_kept_when_it_is_the_whole_query(self):
        self.assertEqual(ls.parse_request("wo ist die"), ("search", "die"))


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.items = ls.parse_inventory(fixture_bytes(), ls.load_config(None))

    def names(self, result):
        return [hit.item.name for hit in result.hits]

    def test_exact_match_first(self):
        result = ls.search(self.items, "galaxy quest")
        self.assertEqual(self.names(result), ["Galaxy Quest", "Galaxy Quest"])
        self.assertFalse(result.fuzzy)

    def test_substring_ranks_after_exact(self):
        result = ls.search(self.items, "galaxy")
        self.assertEqual(set(self.names(result)), {"Galaxy Quest", "Galaxy Rangers"})

    def test_typo_is_found_fuzzy(self):
        result = ls.search(self.items, "galaxy quets")
        self.assertTrue(result.fuzzy)
        self.assertIn("Galaxy Quest", self.names(result))

    def test_umlaut_free_spelling(self):
        self.assertIn("Zurück in die Zukunft", self.names(ls.search(self.items, "zurueck zukunft")))
        self.assertIn("Über Menschen", self.names(ls.search(self.items, "uber menschen")))

    def test_searches_description_column(self):
        result = ls.search(self.items, "tolkien")
        self.assertEqual(set(self.names(result)), {"Der Herr der Ringe – Die Gefährten", "Der Hobbit"})

    def test_tokens_across_columns(self):
        self.assertEqual(self.names(ls.search(self.items, "galaxy dvd")), ["Galaxy Quest"])

    def test_word_start_beats_word_inside(self):
        self.assertEqual(self.names(ls.search(self.items, "hard")), ["Die Hard"])
        self.assertEqual(self.names(ls.search(self.items, "heftklammer")), ["8mm Tacker Heftklammern"])
        self.assertEqual(self.names(ls.search(self.items, "klammern")), ["8mm Tacker Heftklammern"])

    def test_plural_finds_singular(self):
        self.assertEqual(self.names(ls.search(self.items, "netzteile")), ["12V/10A Netzteil"])

    def test_no_hits_with_suggestions(self):
        result = ls.search(self.items, "galaksi kwest")
        self.assertEqual(result.hits, [])
        self.assertIn("Galaxy Quest", result.suggestions)

    def test_no_hits_without_suggestions(self):
        result = ls.search(self.items, "xylophon")
        self.assertEqual(result.hits, [])
        self.assertEqual(result.suggestions, [])

    def test_fuzzy_needs_same_first_letter(self):
        self.assertIn("Zurück in die Zukunft", self.names(ls.search(self.items, "zukumft")))
        self.assertEqual(ls.search(self.items, "pukunft").hits, [])

    def test_unrelated_query_gives_no_junk_suggestions(self):
        self.assertEqual(ls.search(self.items, "tolkin hobit zwerge").suggestions, [])

    def test_empty_query(self):
        self.assertEqual(ls.search(self.items, "  ").hits, [])


class BoxTests(unittest.TestCase):
    def setUp(self):
        self.items = ls.parse_inventory(fixture_bytes(), ls.load_config(None))
        self.config = ls.load_config(None)

    def test_box_by_number(self):
        groups = ls.find_box(self.items, "4", self.config)
        self.assertEqual(list(groups), ["Keller » Kiste 4"])
        self.assertEqual(len(groups["Keller » Kiste 4"]), 4)

    def test_box_by_name(self):
        groups = ls.find_box(self.items, "küchenkram", self.config)
        self.assertEqual(list(groups), ["Keller » Küchenkram"])

    def test_number_does_not_match_other_numbers(self):
        self.assertEqual(ls.find_box(self.items, "1", self.config), {})

    def test_box_output_grouped_and_sorted(self):
        text = ls.answer("kiste 4", self.items, self.config)
        self.assertTrue(text.startswith("📦 Kiste 4 · Keller (4 Items)"))
        lines = [line for line in text.splitlines() if line.startswith("•")]
        self.assertEqual(lines[0], "• A Knights Tale (Bluray)")
        self.assertIn("🎬 Filme", text)

    def test_unknown_box(self):
        text = ls.answer("kiste 99", self.items, self.config)
        self.assertIn("Keine Kiste", text)
        self.assertIn("Kiste 3", text)


class AnswerTests(unittest.TestCase):
    def setUp(self):
        self.config = ls.load_config(None)
        self.items = ls.parse_inventory(fixture_bytes(), self.config)

    def test_single_hit_line(self):
        text = ls.answer("wo ist die hard", self.items, self.config)
        self.assertEqual(text, "📦 Die Hard (Bluray) → Kiste 4 · Keller")

    def test_multiple_hits_have_header(self):
        text = ls.answer("wo ist galaxy quest", self.items, self.config)
        self.assertIn("2 Treffer", text)
        self.assertIn("📦 Galaxy Quest (DVD) → Kiste 3 · Keller", text)
        self.assertIn("📦 Galaxy Quest (Bluray) → Kiste 3 · Keller", text)

    def test_notes_quantity_and_unknown_location(self):
        self.assertIn("📝 Verliehen an Susi", ls.answer("god of war", self.items, self.config))
        self.assertIn("×2", ls.answer("starcraft", self.items, self.config))
        self.assertIn("Ort unbekannt", ls.answer("tomaten", self.items, self.config))

    def test_zero_hits_message(self):
        text = ls.answer("wo ist galaksi kwest", self.items, self.config)
        self.assertIn("Nichts gefunden", text)
        self.assertIn("Meintest du", text)
        self.assertIn("Galaxy Quest", text)

    def test_fuzzy_header(self):
        self.assertIn("ähnlich", ls.answer("galaxy quets", self.items, self.config))

    def test_multiple_terms(self):
        text = ls.answer("hobbit; galaxy quest; xylophon", self.items, self.config)
        blocks = text.split("\n\n")
        self.assertEqual(len(blocks), 3)
        self.assertIn("Der Hobbit", blocks[0])
        self.assertIn("Galaxy Quest", blocks[1])
        self.assertIn("Nichts gefunden für „xylophon“", blocks[2])

    def test_multiple_terms_json(self):
        data = json.loads(ls.answer("hobbit; kiste 4", self.items, self.config, as_json=True))
        self.assertEqual([d["mode"] for d in data], ["search", "box"])

    def test_overview(self):
        text = ls.answer("lager", self.items, self.config)
        self.assertIn("16 Items", text)
        self.assertIn("Kiste 4 · Keller: 4", text)

    def test_truncation(self):
        config = ls.load_config(None)
        config["max_chars"] = 200
        text = ls.answer("bluray", self.items, config)
        self.assertLessEqual(len(text), 200)
        self.assertRegex(text, r"… und \d+ weitere")

    def test_json_output(self):
        data = json.loads(ls.answer("galaxy", self.items, self.config, as_json=True))
        self.assertEqual(data["mode"], "search")
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["items"][0]["container"], "Kiste 3")


def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = ls.load_config(None)
        self.target = self.dir / "inventar.csv"
        self.backup = self.dir / "inventar_alt.csv"

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_csv_into_empty_dir(self):
        src = self.dir / "upload.csv"
        src.write_bytes(fixture_bytes())
        message = ls.import_file(src, self.target, self.config)
        self.assertEqual(self.target.read_bytes(), fixture_bytes())
        self.assertFalse(self.backup.exists())
        self.assertIn("✅ 16 Items", message)
        self.assertIn("5 Kisten", message)
        self.assertIn("3 weitere Orte", message)

    def test_import_nestegg_zip_and_backup(self):
        self.target.write_bytes(b"alt")
        src = self.dir / "upload.bin"
        make_zip(src, {
            "Orte.csv": '"Name","Beschreibung"\n"Keller",""\n',
            "Artikel.csv": fixture_bytes(),
            "Kategorien.csv": '"Name"\n"Filme"\n',
        })
        message = ls.import_file(src, self.target, self.config)
        self.assertEqual(self.target.read_bytes(), fixture_bytes())
        self.assertEqual(self.backup.read_bytes(), b"alt")
        self.assertIn("✅", message)

    def test_zip_without_items_csv_falls_back_to_column_check(self):
        src = self.dir / "upload.zip"
        make_zip(src, {"export/Items.csv": fixture_bytes()})
        ls.import_file(src, self.target, self.config)
        self.assertEqual(self.target.read_bytes(), fixture_bytes())

    def test_invalid_file_keeps_old_inventory(self):
        self.target.write_bytes(fixture_bytes())
        src = self.dir / "upload.csv"
        src.write_bytes(b'"Foo","Bar"\n"1","2"\n')
        with self.assertRaises(ls.InventoryError):
            ls.import_file(src, self.target, self.config)
        self.assertEqual(self.target.read_bytes(), fixture_bytes())
        self.assertFalse(self.backup.exists())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["inventar.csv", "upload.csv"])

    def test_zip_with_wrong_content_is_rejected(self):
        src = self.dir / "upload.zip"
        make_zip(src, {"Orte.csv": '"Name","Beschreibung"\n"Keller",""\n'})
        with self.assertRaises(ls.InventoryError):
            ls.import_file(src, self.target, self.config)

    def test_shrinking_inventory_is_reported(self):
        big = fixture_bytes()
        self.target.write_bytes(big)
        small = b"\n".join(big.split(b"\n")[:3]) + b"\n"
        src = self.dir / "upload.csv"
        src.write_bytes(small)
        message = ls.import_file(src, self.target, self.config)
        self.assertIn("vorher 16", message)
        self.assertIn("⚠️", message)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.data = self.dir / "inventar.csv"
        self.data.write_bytes(fixture_bytes())

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ls.main(["--data", str(self.data), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_search_words_are_joined(self):
        code, out, _ = self.run_cli("wo", "ist", "die", "hard")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "📦 Die Hard (Bluray) → Kiste 4 · Keller")

    def test_double_dash_and_leading_minus(self):
        # shell_command übergibt „-- <text>“, damit Text wie „-x“ keine Option wird
        code, out, _ = self.run_cli("--", "-die", "hard")
        self.assertEqual(code, 0)
        self.assertIn("Die Hard", out)

    def test_missing_inventory(self):
        self.data.unlink()
        code, out, err = self.run_cli("galaxy")
        self.assertEqual(code, 1)
        self.assertIn("Kein Inventar", err)

    def test_import_and_failure_exit_codes(self):
        good = self.dir / "upload.csv"
        good.write_bytes(fixture_bytes())
        self.assertEqual(self.run_cli("--import", str(good))[0], 0)
        bad = self.dir / "bad.csv"
        bad.write_bytes(b"kaputt")
        code, _, err = self.run_cli("--import", str(bad))
        self.assertEqual(code, 1)
        self.assertIn("❌", err)

    def test_config_file_overrides(self):
        cfg = self.dir / "config.json"
        cfg.write_text(json.dumps({"data_file": str(self.data), "max_chars": 50}))
        out = io.StringIO()
        with redirect_stdout(out):
            ls.main(["--config", str(cfg), "bluray"])
        self.assertLessEqual(len(out.getvalue().strip()), 50)


if __name__ == "__main__":
    unittest.main()
