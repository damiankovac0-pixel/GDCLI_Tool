from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest
from unittest import mock

from gdaitrans import bridge
from gdaitrans.level import CompiledLevel


def compiled(name: str = "Socket Original") -> CompiledLevel:
    return CompiledLevel(
        name=name,
        description="Created through the live bridge",
        song_id=1,
        custom_song_id=0,
        level_string="kS38,1;kA2,0;kA3,0;1,1,2,15,3,15;",
        object_count=1,
    )


class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[tuple[dict, bytes]] = []

    def _exchange(self, reply) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            os.chmod(directory, 0o700)
            path = directory / "bridge.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            os.chmod(path, 0o600)
            listener.listen(1)
            server_errors: list[BaseException] = []

            def serve() -> None:
                try:
                    connection, _ = listener.accept()
                    with connection:
                        wire = bytearray()
                        while b"\n" not in wire:
                            chunk = connection.recv(4096)
                            if not chunk:
                                return
                            wire.extend(chunk)
                        frame, separator, trailing = bytes(wire).partition(b"\n")
                        if separator != b"\n" or trailing:
                            raise AssertionError("client request was not exactly one line")
                        request = json.loads(frame.decode("utf-8"))
                        self.requests.append((request, bytes(wire)))
                        connection.sendall(reply(request))
                except BaseException as exc:
                    server_errors.append(exc)
                finally:
                    listener.close()

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            caught: BaseException | None = None
            result: dict | None = None
            try:
                with mock.patch.object(bridge, "socket_path", return_value=path):
                    result = bridge.rpc("status", {"probe": "✓"}, timeout=2)
            except BaseException as exc:
                caught = exc
            finally:
                thread.join(timeout=2)
                listener.close()
            if thread.is_alive():
                self.fail("test bridge server did not finish")
            if server_errors:
                raise server_errors[0]
            if caught is not None:
                raise caught
            assert result is not None
            return result

    def test_rpc_uses_one_json_line_and_accepts_only_matching_object_result(self) -> None:
        result = self._exchange(
            lambda request: json.dumps(
                {"id": request["id"], "result": {"connected": True}}
            ).encode("utf-8")
            + b"\n"
        )

        self.assertEqual(result, {"connected": True})
        request, wire = self.requests[0]
        self.assertEqual(request["method"], "status")
        self.assertEqual(request["params"], {"probe": "✓"})
        self.assertIsInstance(request["id"], str)
        self.assertTrue(request["id"])
        self.assertEqual(wire.count(b"\n"), 1)

    def test_remote_error_is_explicit_bridge_error(self) -> None:
        with self.assertRaisesRegex(bridge.BridgeError, "unsafe scene transition"):
            self._exchange(
                lambda request: json.dumps(
                    {"id": request["id"], "error": "unsafe scene transition"}
                ).encode()
                + b"\n"
            )

    def test_response_id_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(bridge.BridgeProtocolError, "ID"):
            self._exchange(
                lambda request: b'{"id":"different","result":{}}\n'
            )

    def test_duplicate_response_fields_are_rejected(self) -> None:
        with self.assertRaises(bridge.BridgeProtocolError):
            self._exchange(
                lambda request: (
                    f'{{"id":"{request["id"]}","id":"{request["id"]}","result":{{}}}}\n'
                ).encode()
            )

    def test_result_and_error_together_are_rejected(self) -> None:
        with self.assertRaises(bridge.BridgeProtocolError):
            self._exchange(
                lambda request: json.dumps(
                    {"id": request["id"], "result": {}, "error": "ambiguous"}
                ).encode()
                + b"\n"
            )

    def test_framing_rejects_trailing_or_oversized_data(self) -> None:
        trailing = mock.Mock()
        trailing.recv.return_value = b'{"id":"x","result":{}}\n{}\n'
        with self.assertRaisesRegex(bridge.BridgeProtocolError, "after"):
            bridge._read_response(trailing)

        oversized = mock.Mock()
        oversized.recv.side_effect = [b"x" * 17]
        with (
            mock.patch.object(bridge, "_MAX_FRAME_BYTES", 16),
            self.assertRaisesRegex(bridge.BridgeProtocolError, "exceeds"),
        ):
            bridge._read_response(oversized)

    def test_invalid_json_params_fail_before_socket_access(self) -> None:
        with (
            mock.patch.object(bridge, "_validate_socket_endpoint") as endpoint,
            self.assertRaises(bridge.BridgeProtocolError),
        ):
            bridge.rpc("status", {"nan": float("nan")})
        endpoint.assert_not_called()

    def test_socket_must_be_private_and_user_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "bridge.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(path))
                os.chmod(directory, 0o755)
                os.chmod(path, 0o600)
                with self.assertRaisesRegex(bridge.BridgeUnavailableError, "0700"):
                    bridge._validate_socket_endpoint(path)
            finally:
                listener.close()


class StatusAndAuthoringTests(unittest.TestCase):
    def test_status_requires_real_complete_account_and_game_evidence(self) -> None:
        valid = {
            "connected": True,
            "username": "Creator",
            "account_id": 12,
            "user_id": 34,
            "local_level_count": 2,
            "scene": "menu",
            "playing": False,
            "completed": False,
            "deaths": 0,
            "progress": 0.0,
        }
        with mock.patch.object(bridge, "rpc", return_value=valid):
            self.assertIs(bridge.status(), valid)

        for field in valid:
            invalid = dict(valid)
            del invalid[field]
            with (
                self.subTest(field=field),
                mock.patch.object(bridge, "rpc", return_value=invalid),
                self.assertRaises(bridge.BridgeProtocolError),
            ):
                bridge.status()

    def test_create_checkpoints_then_keeps_exact_private_encrypted_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_directory = root / "game"
            save_directory.mkdir()
            encrypted = os.urandom(257)
            (save_directory / "CCLocalLevels.dat").write_bytes(encrypted)
            calls: list[tuple[str, dict | None]] = []

            def rpc(method: str, params: dict | None = None, timeout: float = 15) -> dict:
                calls.append((method, params))
                if method == "checkpoint":
                    return {"saved": True}
                self.assertEqual(method, "create_level")
                assert params is not None
                return {
                    "name": params["name"],
                    "creator": "Creator",
                    "account_id": 12,
                    "user_id": 34,
                    "object_count": params["object_count"],
                    "local_level_count": 3,
                }

            with (
                mock.patch.object(bridge.saves, "default_save_dir", return_value=save_directory),
                mock.patch.object(bridge, "_application_root", return_value=root / "private"),
                mock.patch.object(bridge, "rpc", side_effect=rpc),
            ):
                result = bridge.create_level(compiled())

            backup = Path(result["backup"])
            self.assertEqual(backup.read_bytes(), encrypted)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
            self.assertEqual([call[0] for call in calls], ["checkpoint", "create_level"])
            self.assertEqual(
                set(calls[1][1] or ()),
                {"name", "description", "song_id", "custom_song_id", "level_string", "object_count"},
            )

    def test_create_failure_retains_pre_create_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_directory = root / "game"
            save_directory.mkdir()
            encrypted = b"encrypted-local-level-save"
            (save_directory / "CCLocalLevels.dat").write_bytes(encrypted)

            def rpc(method: str, params: dict | None = None, timeout: float = 15) -> dict:
                if method == "checkpoint":
                    return {"saved": True}
                raise bridge.BridgeError("duplicate name")

            with (
                mock.patch.object(bridge.saves, "default_save_dir", return_value=save_directory),
                mock.patch.object(bridge, "_application_root", return_value=root / "private"),
                mock.patch.object(bridge, "rpc", side_effect=rpc),
                self.assertRaisesRegex(bridge.BridgeError, "duplicate"),
            ):
                bridge.create_level(compiled())

            backups = list((root / "private" / "backups").glob("*.dat"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), encrypted)

    def test_invalid_compiled_level_has_no_checkpoint_or_backup_side_effect(self) -> None:
        invalid = CompiledLevel("Bad", "", 1, 2, "kA1,1;", 0)
        with (
            mock.patch.object(bridge, "rpc") as rpc,
            mock.patch.object(bridge, "_backup_local_levels") as backup,
            self.assertRaises(ValueError),
        ):
            bridge.create_level(invalid)
        rpc.assert_not_called()
        backup.assert_not_called()

    def test_open_and_click_validate_before_rpc(self) -> None:
        with mock.patch.object(bridge, "rpc") as rpc:
            with self.assertRaises(ValueError):
                bridge.open_level("Level", "practice")
            with self.assertRaises(ValueError):
                bridge.click(0.5, 1.1)
        rpc.assert_not_called()


class InputSafetyTests(unittest.TestCase):
    def test_press_attempts_release_when_key_down_response_fails(self) -> None:
        failure = bridge.BridgeUnavailableError("response lost")
        with (
            mock.patch.object(bridge, "rpc", side_effect=[failure, {"released": True}]) as rpc,
            mock.patch.object(bridge.time, "sleep") as sleep,
            self.assertRaises(bridge.BridgeUnavailableError),
        ):
            bridge.press("space")

        self.assertEqual(
            rpc.call_args_list,
            [
                mock.call("input", {"key": "space", "down": True}),
                mock.call("input", {"key": "space", "down": False}),
            ],
        )
        sleep.assert_not_called()

    def test_all_events_validate_before_first_input_side_effect(self) -> None:
        events = [
            {"at": 0, "key": "space", "action": "down"},
            {"at": 0.1, "key": "unsupported", "action": "down"},
        ]
        with (
            mock.patch.object(bridge, "rpc") as rpc,
            self.assertRaises(ValueError),
        ):
            bridge.run_inputs(events, 1)
        rpc.assert_not_called()

    def test_failed_key_up_is_retried_for_every_possibly_held_key(self) -> None:
        failure = bridge.BridgeUnavailableError("lost up response")
        events = [
            {"at": 0, "key": "right", "action": "down"},
            {"at": 0, "key": "space", "action": "down"},
            {"at": 0, "key": "right", "action": "up"},
        ]
        with (
            mock.patch.object(
                bridge,
                "rpc",
                side_effect=[{}, {}, failure, {"released": True}, {"released": True}],
            ) as rpc,
            mock.patch.object(bridge, "_wait_until"),
            self.assertRaises(bridge.BridgeUnavailableError),
        ):
            bridge.run_inputs(events, 1)

        self.assertEqual(
            rpc.call_args_list[-2:],
            [
                mock.call("input", {"key": "right", "down": False}),
                mock.call("input", {"key": "space", "down": False}),
            ],
        )

    def test_capture_rejects_missing_file_and_returns_real_png_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture_directory = root / "captures"
            capture_directory.mkdir()
            image = capture_directory / "capture.png"
            image.write_bytes(
                b"\x89PNG\r\n\x1a\npayload"
                b"\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            result = {"image": str(image), "scene": "editor"}
            with (
                mock.patch.object(bridge, "_application_root", return_value=root),
                mock.patch.object(bridge, "rpc", return_value=result),
            ):
                self.assertIs(bridge.capture(), result)

            image.unlink()
            with (
                mock.patch.object(bridge, "_application_root", return_value=root),
                mock.patch.object(bridge, "rpc", return_value=result),
                self.assertRaises(bridge.BridgeProtocolError),
            ):
                bridge.capture()


class AuthoringOwnershipTests(unittest.TestCase):
    def test_unrelated_level_update_is_refused_before_game_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(bridge, "_application_root", return_value=Path(temporary)),
                mock.patch.object(bridge, "rpc") as rpc,
                self.assertRaisesRegex(bridge.BridgeError, "not created by this tool"),
            ):
                bridge.update_level(compiled("An existing personal level"))
            rpc.assert_not_called()

    def test_account_switch_cannot_overwrite_a_previous_accounts_level(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(bridge, "_application_root", return_value=root):
                bridge._record_authored({"name": "Socket Original", "account_id": 12, "user_id": 34})
                current = {"connected": True, "username": "Different Creator", "account_id": 56,
                           "user_id": 78, "local_level_count": 4, "scene": "menu",
                           "playing": False, "completed": False, "deaths": 0, "progress": 0}
                with (
                    mock.patch.object(bridge, "rpc", return_value=current) as rpc,
                    self.assertRaisesRegex(bridge.BridgeError, "another account"),
                ):
                    bridge.update_level(compiled())
                self.assertEqual([call.args[0] for call in rpc.call_args_list], ["status"])


if __name__ == "__main__":
    unittest.main()
