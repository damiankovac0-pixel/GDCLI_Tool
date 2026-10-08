from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
import os
import stat
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from gdaitrans import saves


EMPTY_LOCAL_XML = (
    '<?xml version="1.0"?><plist version="1.0" gjver="2.0"><d>'
    '<k>LLM_01</k><d><k>_isArr</k><t /></d>'
    '<k>LLM_02</k><i>47</i><k>LLM_03</k><d><k>_isArr</k><t /></d>'
    '</d></plist>'
)

LOCAL_WITH_LEVEL_XML = (
    '<?xml version="1.0"?><plist version="1.0" gjver="2.0"><d>'
    '<k>LLM_01</k><d><k>_isArr</k><t />'
    '<k>k_0</k><d><k>kCEK</k><i>4</i><k>k2</k><s>Existing</s>'
    '<k>k5</k><s>Earlier</s><k>k13</k><t /><k>k21</k><i>2</i>'
    '<k>k48</k><i>9</i><k>unknown</k><s>preserve &amp; exactly</s></d>'
    '</d><k>LLM_02</k><i>47</i>'
    '<k>LLM_03</k><d><k>_isArr</k><t /></d></d></plist>'
)

MANAGER_XML = (
    '<?xml version="1.0"?><plist version="1.0" gjver="2.0"><d>'
    '<k>playerName</k><s>Fallback</s><k>playerUserID</k><i>456</i>'
    '<k>GJA_001</k><s>Creator &amp; Co</s><k>GJA_002</k><s>plaintext-secret</s>'
    '<k>GJA_003</k><i>123</i><k>GJA_005</k><s>token-secret</s>'
    '<k>unknownManagerState</k><s>untouched</s></d></plist>'
)


def compiled(name: str = "Original & New") -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description="A safe <description>",
        song_id=1,
        custom_song_id=0,
        level_string="kS38,1;kA2,0;kA3,0;1,1,2,15,3,15;",
        object_count=1,
    )


class NativeCodecTests(unittest.TestCase):
    def test_native_codec_preserves_xml_and_trailing_whitespace(self) -> None:
        for trailing_tabs in range(16):
            xml = EMPTY_LOCAL_XML + "\t" * trailing_tabs
            with self.subTest(trailing_tabs=trailing_tabs):
                self.assertEqual(saves.decode_save(saves.encode_save(xml)), xml)

    def test_invalid_padding_is_rejected_without_plaintext_trimming(self) -> None:
        encryptor = Cipher(algorithms.AES(saves._AES_KEY), modes.ECB()).encryptor()
        ciphertext = encryptor.update(b"x" * 16) + encryptor.finalize()
        with self.assertRaises(saves.SaveFormatError):
            saves.decode_save(ciphertext)


class SaveReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.save_dir = Path(self.temporary.name)
        (self.save_dir / "CCGameManager.dat").write_bytes(saves.encode_save(MANAGER_XML))
        (self.save_dir / "CCLocalLevels.dat").write_bytes(
            saves.encode_save(LOCAL_WITH_LEVEL_XML)
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_account_info_returns_only_allowlisted_identity(self) -> None:
        self.assertEqual(
            saves.account_info(self.save_dir),
            {
                "username": "Creator & Co",
                "account_id": 123,
                "user_id": 456,
                "signed_in": True,
            },
        )

    def test_list_levels_returns_safe_metadata_only(self) -> None:
        self.assertEqual(
            saves.list_levels(self.save_dir),
            [
                {
                    "name": "Existing",
                    "index": 0,
                    "creator": "Earlier",
                    "object_count": 9,
                    "song_id": 0,
                    "custom_song_id": 0,
                    "user_id": 0,
                    "account_id": 0,
                    "editable": True,
                    "level_type": 2,
                }
            ],
        )


class SafeImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.save_dir = root / "game"
        self.backup_dir = root / "backups"
        self.save_dir.mkdir()
        self.manager_path = self.save_dir / "CCGameManager.dat"
        self.local_path = self.save_dir / "CCLocalLevels.dat"
        self.manager_encrypted = saves.encode_save(MANAGER_XML)
        self.original_encrypted = saves.encode_save(LOCAL_WITH_LEVEL_XML)
        self.manager_path.write_bytes(self.manager_encrypted)
        self.local_path.write_bytes(self.original_encrypted)
        os.chmod(self.local_path, 0o640)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _import(self, level: SimpleNamespace | None = None) -> dict:
        with (
            mock.patch.object(saves.sys, "platform", "darwin"),
            mock.patch.object(saves, "_assert_game_stopped"),
        ):
            return saves.import_level(
                level or compiled(), self.save_dir, self.backup_dir
            )

    def test_import_appends_only_one_entry_and_keeps_exact_encrypted_backup(self) -> None:
        result = self._import()

        self.assertEqual(
            {key: result[key] for key in ("name", "creator", "account_id", "object_count", "index")},
            {
                "name": "Original & New",
                "creator": "Creator & Co",
                "account_id": 123,
                "object_count": 1,
                "index": 1,
            },
        )
        self.assertEqual(Path(result["backup"]).read_bytes(), self.original_encrypted)
        self.assertEqual(Path(result["save_path"]), self.local_path)
        self.assertEqual(self.manager_path.read_bytes(), self.manager_encrypted)
        self.assertEqual(stat.S_IMODE(self.local_path.stat().st_mode), 0o640)

        updated_xml = saves.decode_save(self.local_path.read_bytes()).encode("utf-8")
        original_xml = LOCAL_WITH_LEVEL_XML.encode("utf-8")
        offset = saves._llm_01_insert_offset(original_xml)
        self.assertEqual(updated_xml[:offset], original_xml[:offset])
        self.assertTrue(updated_xml.endswith(original_xml[offset:]))
        inserted = updated_xml[offset : len(updated_xml) - len(original_xml[offset:])]
        self.assertIn(b"<k>k_1</k>", inserted)
        self.assertIn(b"<k>k2</k><s>Original &amp; New</s>", inserted)
        self.assertIn(b"<k>k3</k><s>" + base64.b64encode(b"A safe <description>") + b"</s>", inserted)
        self.assertIn(b"<k>k5</k><s>Creator &amp; Co</s>", inserted)
        self.assertIn(b"<k>k6</k><i>456</i>", inserted)
        self.assertIn(b"<k>k60</k><i>123</i>", inserted)
        self.assertIn(b"<k>k21</k><i>2</i>", inserted)
        self.assertIn(b"<k>k13</k><t />", inserted)
        self.assertIn(b"<k>k50</k><i>47</i>", inserted)
        self.assertNotIn(b"<k>k14</k>", inserted)
        self.assertNotIn(b"<k>k19</k>", inserted)
        self.assertNotIn(b"<k>k20</k>", inserted)

        levels = saves.list_levels(self.save_dir)
        self.assertEqual([level["name"] for level in levels], ["Existing", "Original & New"])
        self.assertEqual(levels[1]["creator"], "Creator & Co")
        self.assertEqual(levels[1]["object_count"], 1)

    def test_duplicate_name_aborts_before_backup_or_write(self) -> None:
        with self.assertRaises(saves.DuplicateLevelError):
            self._import(compiled("Existing"))
        self.assertEqual(self.local_path.read_bytes(), self.original_encrypted)
        self.assertFalse(self.backup_dir.exists())

    def test_unsigned_account_aborts_without_explicit_local_mode(self) -> None:
        unsigned = (
            '<?xml version="1.0"?><plist version="1.0"><d>'
            '<k>playerName</k><s>Player</s><k>playerUserID</k><i>0</i>'
            '</d></plist>'
        )
        self.manager_path.write_bytes(saves.encode_save(unsigned))
        with self.assertRaises(saves.AccountRequiredError):
            self._import()
        self.assertEqual(self.local_path.read_bytes(), self.original_encrypted)
        self.assertFalse(self.backup_dir.exists())

    def test_running_game_aborts_before_read_modify_write(self) -> None:
        with (
            mock.patch.object(saves.sys, "platform", "darwin"),
            mock.patch.object(
                saves,
                "_assert_game_stopped",
                side_effect=saves.GameRunningError("running"),
            ),
            self.assertRaises(saves.GameRunningError),
        ):
            saves.import_level(compiled(), self.save_dir, self.backup_dir)
        self.assertEqual(self.local_path.read_bytes(), self.original_encrypted)
        self.assertFalse(self.backup_dir.exists())

    def test_existing_tool_lock_rejects_concurrent_import(self) -> None:
        lock_path = self.save_dir / ".gdaitrans-import.lock"
        with (
            mock.patch.object(saves.sys, "platform", "darwin"),
            mock.patch.object(saves, "_assert_game_stopped"),
            saves._exclusive_import_lock(lock_path),
            self.assertRaises(saves.ConcurrentImportError),
        ):
            saves.import_level(compiled(), self.save_dir, self.backup_dir)
        self.assertEqual(self.local_path.read_bytes(), self.original_encrypted)
        self.assertFalse(self.backup_dir.exists())

    def test_stale_content_aborts_atomic_replace_and_retains_exact_backup(self) -> None:
        external_xml = EMPTY_LOCAL_XML.replace("<i>47</i>", "<i>48</i>")
        external_encrypted = saves.encode_save(external_xml)
        checks = 0

        def mutate_before_final_check() -> None:
            nonlocal checks
            checks += 1
            if checks == 2:
                self.local_path.write_bytes(external_encrypted)

        with (
            mock.patch.object(saves.sys, "platform", "darwin"),
            mock.patch.object(saves, "_assert_game_stopped", side_effect=mutate_before_final_check),
            self.assertRaises(saves.StaleSaveError),
        ):
            saves.import_level(compiled(), self.save_dir, self.backup_dir)

        self.assertEqual(self.local_path.read_bytes(), external_encrypted)
        backups = list(self.backup_dir.glob("*.dat"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original_encrypted)


if __name__ == "__main__":
    unittest.main()
