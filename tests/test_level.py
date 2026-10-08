from __future__ import annotations

import base64
import gzip
import math
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from gdaitrans.catalog import find_objects, get_object
from gdaitrans.level import compile_level, export_gmd


class CompileLevelTests(unittest.TestCase):
    def test_compiles_real_level_string_in_object_order_with_raw_properties(self) -> None:
        compiled = compile_level(
            {
                "name": "Original",
                "description": "locally authored",
                "objects": [
                    {"id": 1, "x": 15, "y": 30, "properties": {"24": 7, "4": True}},
                    {"id": 8, "x": 45.5, "y": 30, "properties": {"999": "advanced"}},
                ],
            }
        )

        self.assertEqual(compiled.object_count, 2)
        self.assertTrue(compiled.level_string.endswith(
            ";1,1,2,15,3,30,4,1,24,7;1,8,2,45.5,3,30,999,advanced;"
        ))
        self.assertIn("kA2,0,kA3,0,kA4,0", compiled.level_string)
        self.assertIn("kA22,0", compiled.level_string)

    def test_rejects_nonfinite_coordinates_with_object_index(self) -> None:
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, r"spec\.objects\[1\]\.x must be finite"):
                    compile_level(
                        {
                            "name": "Unsafe",
                            "objects": [
                                {"id": 1, "x": 0, "y": 0},
                                {"id": 1, "x": value, "y": 0},
                            ],
                        }
                    )

    def test_rejects_typed_property_conflicts_and_delimiters(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflicts with typed field"):
            compile_level(
                {
                    "name": "Conflict",
                    "objects": [
                        {"id": 1, "x": 0, "y": 0, "properties": {"2": 100}}
                    ],
                }
            )
        with self.assertRaisesRegex(ValueError, "unsafe level-string delimiter"):
            compile_level(
                {
                    "name": "Delimiter",
                    "objects": [
                        {"id": 1, "x": 0, "y": 0, "properties": {"31": "bad,value"}}
                    ],
                }
            )

    def test_rejects_ambiguous_song_selection(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot both be non-zero"):
            compile_level(
                {
                    "name": "Two songs",
                    "song_id": 1,
                    "custom_song_id": 123,
                    "objects": [],
                }
            )


class GmdExportTests(unittest.TestCase):
    def test_exports_deterministic_gdshare_plist_and_decodable_k4(self) -> None:
        compiled = compile_level(
            {
                "name": "GMD & XML",
                "description": "description ✓",
                "custom_song_id": 12345,
                "objects": [{"id": 35, "x": 90, "y": 30}],
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            first = export_gmd(compiled, Path(temporary) / "first.gmd")
            second = export_gmd(compiled, Path(temporary) / "second.gmd")
            self.assertEqual(first.read_bytes(), second.read_bytes())

            root = ET.parse(first).getroot().find("dict")
            self.assertIsNotNone(root)
            children = list(root)
            values = {
                children[index].text: children[index + 1]
                for index in range(0, len(children), 2)
            }
            self.assertEqual(values["kCEK"].text, "4")
            self.assertEqual(values["k21"].text, "2")
            self.assertEqual(values["k45"].text, "12345")
            self.assertEqual(values["k48"].text, "1")
            self.assertIn("k13", values)
            self.assertNotIn("k14", values)
            decoded = gzip.decompress(base64.urlsafe_b64decode(values["k4"].text)).decode()
            self.assertEqual(decoded, compiled.level_string)


class CatalogTests(unittest.TestCase):
    def test_known_gmdkit_objects_are_available_by_id_and_name(self) -> None:
        self.assertEqual(get_object(1)["name"], "Black Gradient Square")
        self.assertEqual(get_object(8)["name"], "Black Gradient Spike")
        self.assertEqual(get_object(35)["alias"], "pad.YELLOW")
        self.assertEqual(find_objects("yellow orb", limit=1)[0]["id"], 36)

    def test_catalog_results_are_defensive_copies(self) -> None:
        result = get_object(1)
        result["name"] = "changed"
        self.assertEqual(get_object(1)["name"], "Black Gradient Square")


if __name__ == "__main__":
    unittest.main()
