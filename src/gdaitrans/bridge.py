"""Strict client for the in-game GDAITRANS Unix-socket bridge.

All game authoring and control happens inside Geometry Dash.  This module never
posts operating-system input, activates an application, or falls back to save
editing when the bridge is unavailable.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import stat
import threading
import time
import tempfile
from typing import Any, NoReturn
import uuid

from . import saves
from .level import CompiledLevel

_MAX_FRAME_BYTES = 16 * 1024 * 1024
_SUPPORTED_METHODS = frozenset(
    ("status", "checkpoint", "create_level", "update_level", "open_level", "leave_level", "input", "click", "capture")
)
_SUPPORTED_KEYS = frozenset(
    ("space", "left", "right", "escape", "enter", "tab", "e", "c", "r", "up", "down")
)
_TIMING_DESCRIPTION = "OS wall-clock; not physics-step deterministic"
_CREATE_LOCK = threading.RLock()


class BridgeError(RuntimeError):
    """Base class for explicit bridge and remote-operation failures."""


class BridgeUnavailableError(BridgeError):
    """Raised when the local in-game bridge cannot be reached safely."""


class BridgeProtocolError(BridgeError):
    """Raised when bridge framing, identifiers, or value types are invalid."""


def _application_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "GDAITRANS"


def socket_path() -> Path:
    """Return the private Unix-domain socket used by the in-game bridge."""
    return _application_root() / "bridge.sock"


def _validate_timeout(timeout: float) -> float:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a finite positive number of seconds")
    return float(timeout)


def _validate_socket_endpoint(path: Path) -> None:
    try:
        directory_stat = path.parent.stat(follow_symlinks=False)
        endpoint_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise BridgeUnavailableError(f"Geometry Dash bridge is not available at {path}") from exc
    except OSError as exc:
        raise BridgeUnavailableError(f"could not inspect Geometry Dash bridge at {path}") from exc

    uid = os.getuid()
    directory_mode = stat.S_IMODE(directory_stat.st_mode)
    endpoint_mode = stat.S_IMODE(endpoint_stat.st_mode)
    if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != uid:
        raise BridgeUnavailableError("bridge directory is not a user-owned directory")
    if directory_mode != 0o700:
        raise BridgeUnavailableError("bridge directory permissions must be 0700")
    if not stat.S_ISSOCK(endpoint_stat.st_mode) or endpoint_stat.st_uid != uid:
        raise BridgeUnavailableError("bridge endpoint is not a user-owned Unix socket")
    if endpoint_mode != 0o600:
        raise BridgeUnavailableError("bridge socket permissions must be 0600")


def _validate_peer(connection: socket.socket) -> None:
    getpeereid = getattr(connection, "getpeereid", None)
    if getpeereid is None:
        return
    try:
        peer_uid, _ = getpeereid()
    except OSError as exc:
        raise BridgeUnavailableError("could not authenticate the bridge process") from exc
    if peer_uid != os.getuid():
        raise BridgeUnavailableError("bridge process belongs to a different user")


def _encode_request(request_id: str, method: str, params: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            {"id": request_id, "method": method, "params": params},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise BridgeProtocolError("request parameters are not valid finite JSON values") from exc
    if len(payload) > _MAX_FRAME_BYTES:
        raise BridgeProtocolError("bridge request exceeds the 16 MiB framing limit")
    return payload + b"\n"


def _read_response(connection: socket.socket) -> bytes:
    received = bytearray()
    while True:
        try:
            chunk = connection.recv(
                min(64 * 1024, _MAX_FRAME_BYTES + 2 - len(received))
            )
        except (TimeoutError, socket.timeout) as exc:
            raise BridgeUnavailableError(
                "timed out waiting for the Geometry Dash bridge"
            ) from exc
        except OSError as exc:
            raise BridgeUnavailableError(
                "failed while reading from the Geometry Dash bridge"
            ) from exc
        if not chunk:
            break
        received.extend(chunk)
        if len(received) > _MAX_FRAME_BYTES + 1:
            raise BridgeProtocolError(
                "bridge response exceeds the 16 MiB framing limit"
            )
        newline = received.find(b"\n")
        if newline >= 0 and newline != len(received) - 1:
            raise BridgeProtocolError("bridge sent data after its response frame")
        if newline < 0 and len(received) > _MAX_FRAME_BYTES:
            raise BridgeProtocolError(
                "bridge response exceeds the 16 MiB framing limit"
            )

    if not received or received[-1:] != b"\n":
        raise BridgeProtocolError(
            "bridge closed the connection before a complete response"
        )
    if len(received) == 1:
        raise BridgeProtocolError("bridge returned an empty response frame")
    return bytes(received[:-1])


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object field {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number {value}")


def _decode_response(frame: bytes, request_id: str) -> dict[str, Any]:
    try:
        decoded = frame.decode("utf-8")
        response = json.loads(
            decoded,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError) as exc:
        raise BridgeProtocolError("bridge returned malformed UTF-8 JSON") from exc
    if not isinstance(response, dict):
        raise BridgeProtocolError("bridge response must be a JSON object")
    if response.get("id") != request_id:
        raise BridgeProtocolError("bridge response ID does not match the request ID")

    keys = set(response)
    if keys == {"id", "result"}:
        result = response["result"]
        if not isinstance(result, dict):
            raise BridgeProtocolError("bridge result must be a JSON object")
        return result
    if keys == {"id", "error"}:
        message = response["error"]
        if not isinstance(message, str) or not message:
            raise BridgeProtocolError("bridge error must be a non-empty string")
        raise BridgeError(message)
    raise BridgeProtocolError("bridge response must contain exactly one result or error")


def rpc(method: str, params: dict | None = None, timeout: float = 15) -> dict:
    """Make one strict, newline-framed RPC call to the local in-game bridge."""
    if not isinstance(method, str) or method not in _SUPPORTED_METHODS:
        raise ValueError(f"unsupported bridge method {method!r}")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise TypeError("params must be an object")
    timeout_seconds = _validate_timeout(timeout)
    request_id = uuid.uuid4().hex
    request = _encode_request(request_id, method, params)
    path = socket_path()
    _validate_socket_endpoint(path)

    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as exc:
        raise BridgeUnavailableError("could not create a Unix socket for the bridge") from exc
    try:
        connection.settimeout(timeout_seconds)
        try:
            connection.connect(str(path))
            _validate_peer(connection)
            connection.sendall(request)
        except (TimeoutError, socket.timeout) as exc:
            raise BridgeUnavailableError("timed out connecting to the Geometry Dash bridge") from exc
        except BridgeError:
            raise
        except OSError as exc:
            raise BridgeUnavailableError("could not communicate with the Geometry Dash bridge") from exc
        frame = _read_response(connection)
    finally:
        connection.close()
    return _decode_response(frame, request_id)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_integer(result: dict, field: str, *, positive: bool = False) -> int:
    if field not in result or not _is_integer(result[field]):
        raise BridgeProtocolError(f"bridge result field {field!r} must be an integer")
    value = result[field]
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise BridgeProtocolError(f"bridge result field {field!r} must be {qualifier}")
    return value


def _require_nonempty_string(result: dict, field: str) -> str:
    if field not in result or not isinstance(result[field], str) or not result[field]:
        raise BridgeProtocolError(f"bridge result field {field!r} must be a non-empty string")
    return result[field]


def _validated_status(result: dict) -> dict:
    if result.get("connected") is not True:
        raise BridgeProtocolError("bridge status must explicitly report connected=true")
    _require_nonempty_string(result, "username")
    _require_integer(result, "account_id", positive=True)
    _require_integer(result, "user_id", positive=True)
    _require_integer(result, "local_level_count")
    _require_nonempty_string(result, "scene")
    _require_integer(result, "deaths")
    for field in ("playing", "completed"):
        if field not in result or not isinstance(result[field], bool):
            raise BridgeProtocolError(f"bridge status field {field!r} must be a boolean")
    progress = result.get("progress")
    if (
        isinstance(progress, bool)
        or not isinstance(progress, (int, float))
        or not math.isfinite(progress)
        or progress < 0
    ):
        raise BridgeProtocolError("bridge status field 'progress' must be a finite non-negative number")
    if "level_name" in result and (
        not isinstance(result["level_name"], str) or not result["level_name"]
    ):
        raise BridgeProtocolError("bridge status field 'level_name' must be a non-empty string")
    return result


def status() -> dict:
    """Return strict, real account and game state from the running bridge."""
    return _validated_status(rpc("status"))


def _ensure_private_directory(directory: Path) -> None:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_stat = directory.stat(follow_symlinks=False)
    except OSError as exc:
        raise BridgeError(f"could not create private backup directory {directory}") from exc
    if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != os.getuid():
        raise BridgeError(f"backup path is not a user-owned directory: {directory}")
    if stat.S_IMODE(directory_stat.st_mode) != 0o700:
        raise BridgeError(f"backup directory permissions must be 0700: {directory}")


def _backup_local_levels() -> Path:
    save_path = saves.default_save_dir() / "CCLocalLevels.dat"
    application_root = _application_root()
    backup_directory = application_root / "backups"
    _ensure_private_directory(application_root)
    _ensure_private_directory(backup_directory)
    try:
        encrypted, _ = saves._read_regular_file(save_path)
        if not encrypted:
            raise BridgeError("the local-level save is empty; no level was created")
        digest = hashlib.sha256(encrypted).digest()
        backup = saves._write_backup(backup_directory, encrypted, digest)
        saves._assert_unchanged(save_path, digest)
    except saves.SaveError as exc:
        # A completed snapshot is intentionally retained if the source changes
        # after it is copied and before level creation begins.
        raise BridgeError("could not retain an exact encrypted pre-create backup") from exc
    return backup


def _validated_compiled_params(compiled: CompiledLevel) -> dict[str, Any]:
    if not isinstance(compiled, CompiledLevel):
        raise TypeError("compiled must be a CompiledLevel")
    params = saves._validated_compiled_level(compiled)
    if params["song_id"] and params["custom_song_id"]:
        raise ValueError("compiled level cannot select both a built-in and custom song")
    return params


def _validated_create_result(result: dict, expected: dict[str, Any]) -> dict:
    name = _require_nonempty_string(result, "name")
    _require_nonempty_string(result, "creator")
    _require_integer(result, "account_id", positive=True)
    _require_integer(result, "user_id", positive=True)
    object_count = _require_integer(result, "object_count")
    _require_integer(result, "local_level_count")
    if name != expected["name"]:
        raise BridgeProtocolError("bridge created a level with an unexpected name")
    if object_count != expected["object_count"]:
        raise BridgeProtocolError("bridge reported an unexpected object count")
    return result


@contextlib.contextmanager
def _authoring_lock():
    """Keep checkpoint/backup/mutation/ownership ordered across CLI clients."""
    with _CREATE_LOCK:
        root = _application_root()
        _ensure_private_directory(root)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(root / "authoring.lock", flags, 0o600)
            lock_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != os.getuid()
                or stat.S_IMODE(lock_stat.st_mode) != 0o600
            ):
                raise BridgeError(
                    "the authoring lock is not a private user-owned regular file"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except BridgeError:
            if "descriptor" in locals():
                os.close(descriptor)
            raise
        except OSError as exc:
            if "descriptor" in locals():
                os.close(descriptor)
            raise BridgeError("could not acquire the private authoring lock") from exc
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _authored_levels() -> dict[str, dict[str, int]]:
    path = _application_root() / "authored-levels.json"
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise BridgeError("could not safely open the authored-level registry") from exc
    try:
        registry_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(registry_stat.st_mode)
            or registry_stat.st_uid != os.getuid()
            or stat.S_IMODE(registry_stat.st_mode) != 0o600
            or registry_stat.st_size > 1024 * 1024
        ):
            raise BridgeError(
                "the authored-level registry is not a bounded private user-owned file"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise BridgeError("could not read the authored-level registry") from exc
    finally:
        os.close(descriptor)

    try:
        data = json.loads(
            b"".join(chunks).decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError) as exc:
        raise BridgeError("the private authored-level registry is invalid") from exc
    if not isinstance(data, dict):
        raise BridgeError("the private authored-level registry is invalid")
    for name, owner in data.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(owner, dict)
            or set(owner) != {"account_id", "user_id"}
            or not _is_integer(owner["account_id"])
            or owner["account_id"] <= 0
            or not _is_integer(owner["user_id"])
            or owner["user_id"] <= 0
        ):
            raise BridgeError("the private authored-level registry is invalid")
    return data


def _record_authored(result: dict) -> None:
    records = _authored_levels()
    records[result["name"]] = {
        "account_id": result["account_id"],
        "user_id": result["user_id"],
    }
    root = _application_root()
    try:
        descriptor, name = tempfile.mkstemp(prefix=".authored-", dir=root)
    except OSError as exc:
        raise BridgeError("could not create a private authored-level registry") from exc
    temporary = Path(name)
    try:
        payload = (
            json.dumps(
                records,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        saves._write_all(descriptor, payload)
        saves._sync_file(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, root / "authored-levels.json")
        saves._sync_directory(root)
    except (OSError, saves.SaveError) as exc:
        raise BridgeError("could not durably save the authored-level registry") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def create_level(compiled: CompiledLevel) -> dict:
    """Checkpoint, back up, then create a new level through the live game."""
    params = _validated_compiled_params(compiled)
    with _authoring_lock():
        rpc("checkpoint")
        backup = _backup_local_levels()
        result = _validated_create_result(rpc("create_level", params), params)
        _record_authored(result)
    return {**result, "backup": str(backup)}


def update_level(compiled: CompiledLevel) -> dict:
    """Revise only a level previously created by this tool in this account."""
    params = _validated_compiled_params(compiled)
    with _authoring_lock():
        owner = _authored_levels().get(compiled.name)
        if not isinstance(owner, dict):
            raise BridgeError("update refused: this level was not created by this tool")
        current = status()
        if owner != {"account_id": current["account_id"], "user_id": current["user_id"]}:
            raise BridgeError("update refused: the authored level belongs to another account")
        rpc("checkpoint")
        backup = _backup_local_levels()
        result = _validated_create_result(rpc("update_level", params), params)
    return {**result, "backup": str(backup)}


def open_level(name: str, mode: str = "editor") -> dict:
    """Open an exact local level in the real editor or normal play scene."""
    if not isinstance(name, str):
        raise TypeError("name must be a string")
    if not name.strip():
        raise ValueError("name must not be empty")
    if mode not in ("editor", "play"):
        raise ValueError("mode must be 'editor' or 'play'")
    return rpc("open_level", {"name": name, "mode": mode})


def leave_level(timeout: float = 10.0) -> dict:
    """Use normal save/exit for the editor or quit for play; never discard edits."""
    _validate_timeout(timeout)
    result = rpc("leave_level")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = status()
        if current["scene"] not in ("editor", "play"):
            return {**result, "state": current}
        time.sleep(0.05)
    raise BridgeError("the game did not finish its normal level exit")


def click(x: float, y: float) -> dict:
    """Activate an in-scene menu item at normalized, top-left coordinates."""
    for value, field in ((x, "x"), (y, "y")):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ValueError(f"{field} must be finite and between 0 and 1")
    return rpc("click", {"x": x, "y": y})


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or key not in _SUPPORTED_KEYS:
        choices = ", ".join(sorted(_SUPPORTED_KEYS))
        raise ValueError(f"unsupported key {key!r}; choose from {choices}")
    return key


def _validate_hold(hold: float) -> float:
    if (
        isinstance(hold, bool)
        or not isinstance(hold, (int, float))
        or not math.isfinite(hold)
        or not 0 <= hold <= 30
    ):
        raise ValueError("hold must be finite and between 0 and 30 seconds")
    return float(hold)


def _raise_primary(primary: BaseException, release_errors: list[BaseException]) -> NoReturn:
    if release_errors:
        primary.add_note(
            "one or more bridge key-release calls also failed: "
            + "; ".join(str(error) for error in release_errors)
        )
        raise primary.with_traceback(primary.__traceback__) from release_errors[0]
    raise primary.with_traceback(primary.__traceback__)


def press(key: str, hold: float = 0.08) -> dict:
    """Press and reliably release one supported in-game key."""
    key = _validate_key(key)
    hold_seconds = _validate_hold(hold)
    down_result: dict | None = None
    primary: BaseException | None = None
    try:
        down_result = rpc("input", {"key": key, "down": True})
        time.sleep(hold_seconds)
    except BaseException as exc:
        primary = exc

    release_errors: list[BaseException] = []
    up_result: dict | None = None
    try:
        up_result = rpc("input", {"key": key, "down": False})
    except BaseException as exc:
        release_errors.append(exc)

    if primary is not None:
        _raise_primary(primary, release_errors)
    if release_errors:
        raise release_errors[0]
    return {
        "key": key,
        "hold_seconds": hold_seconds,
        "down": down_result,
        "up": up_result,
    }


def _validated_capture(result: dict) -> dict:
    image = result.get("image")
    if not isinstance(image, str) or not image:
        raise BridgeProtocolError("bridge capture must include a non-empty image path")
    _require_nonempty_string(result, "scene")
    if "level_name" in result and (
        not isinstance(result["level_name"], str) or not result["level_name"]
    ):
        raise BridgeProtocolError("bridge capture field 'level_name' must be a non-empty string")
    path = Path(image)
    if not path.is_absolute() or path.suffix.lower() != ".png":
        raise BridgeProtocolError("bridge capture image must be an absolute PNG path")
    normalized = Path(os.path.abspath(path))
    capture_root = Path(os.path.abspath(_application_root() / "captures"))
    if not normalized.is_relative_to(capture_root):
        raise BridgeProtocolError("bridge capture image must be in the private captures directory")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(normalized, flags)
        image_stat = os.fstat(descriptor)
        signature = os.read(descriptor, 8)
        if image_stat.st_size >= 20:
            os.lseek(descriptor, -12, os.SEEK_END)
            ending = os.read(descriptor, 12)
        else:
            ending = b""
    except OSError as exc:
        raise BridgeProtocolError("bridge capture image is not a completed readable file") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not stat.S_ISREG(image_stat.st_mode)
        or image_stat.st_uid != os.getuid()
        or signature != b"\x89PNG\r\n\x1a\n"
        or ending != b"\x00\x00\x00\x00IEND\xaeB`\x82"
    ):
        raise BridgeProtocolError("bridge capture image is not a completed private PNG file")
    return result


def capture() -> dict:
    """Capture the real current game scene and return its private PNG path."""
    return _validated_capture(rpc("capture"))


def _validate_run(events: list[dict], duration: float) -> tuple[list[dict[str, Any]], float]:
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or not 0 < duration <= 600
    ):
        raise ValueError("duration must be finite and between 0 and 600 seconds")
    if not isinstance(events, list):
        raise TypeError("events must be an array")

    validated: list[dict[str, Any]] = []
    previous = -1.0
    held: set[str] = set()
    for index, event in enumerate(events):
        if not isinstance(event, dict) or set(event) != {"at", "key", "action"}:
            raise ValueError(f"events[{index}] needs exactly at, key, and action")
        at = event["at"]
        key = event["key"]
        action = event["action"]
        if (
            isinstance(at, bool)
            or not isinstance(at, (int, float))
            or not math.isfinite(at)
            or at < 0
            or at < previous
            or at > duration
        ):
            raise ValueError(f"events[{index}].at must be ordered and within the run duration")
        _validate_key(key)
        if action not in ("down", "up"):
            raise ValueError(f"events[{index}].action must be 'down' or 'up'")
        if (action == "down") == (key in held):
            raise ValueError(f"events[{index}] repeats a key state rather than changing it")
        if action == "down":
            held.add(key)
        else:
            held.remove(key)
        previous = float(at)
        validated.append({"at": float(at), "key": key, "action": action})
    return validated, float(duration)


def _wait_until(deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def _release_held(held: set[str]) -> list[BaseException]:
    errors: list[BaseException] = []
    for key in sorted(held):
        try:
            rpc("input", {"key": key, "down": False})
        except BaseException as exc:
            errors.append(exc)
    held.clear()
    return errors


def run_inputs(events: list[dict], duration: float) -> dict:
    """Run validated in-game inputs on an honest OS wall-clock timeline.

    Every event is validated before the first RPC.  Any key that might have
    reached the game in a down state is sent a release even when an RPC fails.
    """
    validated, duration_seconds = _validate_run(events, duration)
    started = time.monotonic()
    held: set[str] = set()
    observed: list[dict[str, Any]] = []
    primary: BaseException | None = None

    try:
        for event in validated:
            _wait_until(started + event["at"])
            key = event["key"]
            down = event["action"] == "down"
            if down:
                # Track before the call: a lost response may still mean the
                # native bridge applied the key-down action.
                held.add(key)
                rpc("input", {"key": key, "down": True})
            else:
                rpc("input", {"key": key, "down": False})
                held.remove(key)
            observed.append(
                {
                    **event,
                    "observed_at": round(time.monotonic() - started, 6),
                }
            )
        _wait_until(started + duration_seconds)
    except BaseException as exc:
        primary = exc

    release_errors = _release_held(held)
    if primary is not None:
        _raise_primary(primary, release_errors)
    if release_errors:
        raise release_errors[0]

    elapsed = round(time.monotonic() - started, 6)
    evidence = status()
    screenshot = capture()
    return {
        "duration_seconds": elapsed,
        "timing": _TIMING_DESCRIPTION,
        "events": observed,
        "status": evidence,
        "capture": screenshot,
        "image": screenshot["image"],
    }
