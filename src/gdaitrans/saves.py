"""Safe access to Geometry Dash's native macOS save files.

The AES key and macOS codec are independently implemented from the public
ISC-licensed G.js reader:
https://github.com/g-js-api/G.js/blob/main/reader.js

Level and account property names follow the community-maintained GD Docs:
https://github.com/Wyliemaster/gddocs/blob/master/docs/resources/client/level.md
https://github.com/Wyliemaster/gddocs/blob/master/docs/resources/client/gamesave.md
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import fcntl
import gzip
import hashlib
import html
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, Iterator
import xml.etree.ElementTree as ET
from xml.parsers import expat

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

if TYPE_CHECKING:
    from .level import CompiledLevel

_AES_KEY = b"ipu9TUv54yv]isFMh5@;t.5w34E2Ry@{"
_BLOCK_BITS = 128
_LEVEL_KEY = re.compile(r"k_(\d+)\Z")
_KEY_TAGS = frozenset(("k", "key"))
_DICT_TAGS = frozenset(("d", "dict"))
_STRING_TAGS = frozenset(("s", "string"))
_INT_TAGS = frozenset(("i", "integer"))
_TRUE_TAGS = frozenset(("t", "true"))
_FALSE_TAGS = frozenset(("f", "false"))


class SaveError(RuntimeError):
    """Base class for save access failures."""


class UnsupportedPlatformError(SaveError):
    """Raised when a native save operation is requested off macOS."""


class SaveFormatError(SaveError):
    """Raised when an encrypted save or its XML structure is invalid."""


class GameRunningError(SaveError):
    """Raised when Geometry Dash is running during a requested import."""


class ConcurrentImportError(SaveError):
    """Raised when another GDAITRANS import owns the save lock."""


class StaleSaveError(SaveError):
    """Raised when the local-level save changes during an import."""


class AccountRequiredError(SaveError):
    """Raised when account attribution is required but unavailable."""


class DuplicateLevelError(SaveError):
    """Raised instead of overwriting a local level with the same name."""


def default_save_dir() -> Path:
    """Return the current user's native Geometry Dash save directory."""
    if sys.platform != "darwin":
        raise UnsupportedPlatformError(
            "native Geometry Dash save access is currently supported only on macOS"
        )
    return Path.home() / "Library" / "Application Support" / "GeometryDash"


def decode_save(data: bytes) -> str:
    """Decrypt the game's native macOS AES/PKCS7 save without trimming XML.

    Historical G.js double-padding is deliberately not guessed: padding made
    of tabs/newlines is indistinguishable from legitimate XML whitespace.
    """
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    return _decrypt_save_bytes(data).decode("utf-8")


def encode_save(xml: str) -> bytes:
    """Encrypt XML using the native macOS AES-256-ECB/PKCS7 codec."""
    if not isinstance(xml, str):
        raise TypeError("xml must be a string")
    xml_bytes = xml.encode("utf-8")
    return _encrypt_save_bytes(xml_bytes)


def account_info(save_dir: Path | None = None) -> dict:
    """Return only the documented, non-secret local account identity fields."""
    directory = _save_directory(save_dir)
    xml = _decrypt_save_bytes(_read_regular_file(directory / "CCGameManager.dat")[0])
    values = _extract_allowed_root_values(
        xml, frozenset(("GJA_001", "GJA_003", "playerName", "playerUserID"))
    )

    account_username = _primitive_string(values.get("GJA_001")).strip()
    player_name = _primitive_string(values.get("playerName")).strip()
    account_id = _primitive_int(values.get("GJA_003"), 0)
    user_id = _primitive_int(values.get("playerUserID"), 0)
    username = account_username or player_name
    signed_in = bool(account_username and account_id > 0)
    return {
        "username": username,
        "account_id": account_id,
        "user_id": user_id,
        "signed_in": signed_in,
    }


def list_levels(save_dir: Path | None = None) -> list[dict]:
    """List safe metadata for local editor levels without exposing level data."""
    directory = _save_directory(save_dir)
    xml = _decrypt_save_bytes(_read_regular_file(directory / "CCLocalLevels.dat")[0])
    root = _root_dict(_parse_xml(xml))
    local = _local_level_array(root)

    levels: list[dict] = []
    seen_indices: set[int] = set()
    for key, value in _pairs(local):
        match = _LEVEL_KEY.fullmatch(key)
        if match is None:
            continue
        if _local_name(value) not in _DICT_TAGS:
            raise SaveFormatError(f"local level {key} is not a dictionary")
        index = int(match.group(1))
        if index in seen_indices:
            raise SaveFormatError(f"duplicate local level index {index}")
        seen_indices.add(index)
        fields = _selected_pairs(
            value,
            frozenset(("k2", "k5", "k6", "k8", "k13", "k21", "k45", "k48", "k60")),
        )
        levels.append(
            {
                "name": _as_string(fields.get("k2")),
                "index": index,
                "creator": _as_string(fields.get("k5")),
                "object_count": _as_int(fields.get("k48"), 0),
                "song_id": _as_int(fields.get("k8"), 0),
                "custom_song_id": _as_int(fields.get("k45"), 0),
                "user_id": _as_int(fields.get("k6"), 0),
                "account_id": _as_int(fields.get("k60"), 0),
                "editable": _as_bool(fields.get("k13"), False),
                "level_type": _as_int(fields.get("k21"), 0),
            }
        )
    return levels


def import_level(
    compiled: CompiledLevel,
    save_dir: Path | None = None,
    backup_dir: Path | None = None,
    allow_local: bool = False,
) -> dict:
    """Append one original level with a backup and atomic stale-safe replace."""
    if sys.platform != "darwin":
        raise UnsupportedPlatformError("native level import is supported only on macOS")

    directory = _save_directory(save_dir)
    save_path = directory / "CCLocalLevels.dat"
    backup_directory = (
        Path(backup_dir).expanduser()
        if backup_dir is not None
        else Path.home() / "Library" / "Application Support" / "GDAITRANS" / "backups"
    )
    level = _validated_compiled_level(compiled)

    with _exclusive_import_lock(directory / ".gdaitrans-import.lock"):
        _assert_game_stopped()
        encrypted_original, original_stat = _read_regular_file(save_path)
        original_hash = hashlib.sha256(encrypted_original).digest()
        xml_original = _decrypt_save_bytes(encrypted_original)
        parsed = _parse_xml(xml_original)
        root = _root_dict(parsed)
        local = _local_level_array(root)

        identity = account_info(directory)
        if not allow_local and not identity["signed_in"]:
            raise AccountRequiredError(
                "sign in to Geometry Dash before importing, or explicitly use allow_local=True"
            )
        if not allow_local and identity["signed_in"] and identity["user_id"] <= 0:
            raise AccountRequiredError(
                "the signed-in account has no usable player user ID in CCGameManager.dat"
            )
        creator = identity["username"]
        if not creator:
            raise AccountRequiredError("no local Geometry Dash player name is available")
        _validate_xml_text(creator, "creator")

        indices: set[int] = set()
        existing_names: set[str] = set()
        for key, value in _pairs(local):
            match = _LEVEL_KEY.fullmatch(key)
            if match is None:
                continue
            index = int(match.group(1))
            if index in indices:
                raise SaveFormatError(f"duplicate local level index {index}")
            indices.add(index)
            if _local_name(value) not in _DICT_TAGS:
                raise SaveFormatError(f"local level {key} is not a dictionary")
            name_value = _selected_pairs(value, frozenset(("k2",))).get("k2")
            existing_names.add(_as_string(name_value))
        if level["name"] in existing_names:
            raise DuplicateLevelError(
                f'a local level named "{level["name"]}" already exists; no save was changed'
            )

        index = max(indices, default=-1) + 1
        binary_version = _as_int(_selected_pairs(root, frozenset(("LLM_02",))).get("LLM_02"), 0)
        entry = _build_level_entry(
            index=index,
            creator=creator,
            account_id=identity["account_id"],
            user_id=identity["user_id"],
            binary_version=binary_version,
            **level,
        )
        insertion_offset = _llm_01_insert_offset(xml_original)
        xml_updated = xml_original[:insertion_offset] + entry + xml_original[insertion_offset:]
        _parse_xml(xml_updated)
        encrypted_updated = _encrypt_save_bytes(xml_updated)

        backup_path = _write_backup(backup_directory, encrypted_original, original_hash)
        try:
            _write_atomic_replacement(
                save_path=save_path,
                data=encrypted_updated,
                mode=stat.S_IMODE(original_stat.st_mode),
                original_hash=original_hash,
            )
        except Exception:
            # A completed backup is intentionally retained when a later safety
            # check aborts; it remains the exact pre-import recovery point.
            raise

    return {
        "name": level["name"],
        "creator": creator,
        "account_id": identity["account_id"],
        "object_count": level["object_count"],
        "backup": str(backup_path),
        "save_path": str(save_path),
        "index": index,
    }


def _save_directory(save_dir: Path | None) -> Path:
    return default_save_dir() if save_dir is None else Path(save_dir).expanduser()


def _decrypt_save_bytes(data: bytes) -> bytes:
    if not data or len(data) % 16:
        raise SaveFormatError("native save ciphertext must be a non-empty multiple of 16 bytes")
    try:
        decryptor = Cipher(algorithms.AES(_AES_KEY), modes.ECB()).decryptor()
        padded = decryptor.update(data) + decryptor.finalize()
    except ValueError as exc:
        raise SaveFormatError("could not decrypt native save") from exc

    once = _remove_pkcs7(padded)
    if _is_complete_xml(once):
        return once

    raise SaveFormatError("decrypted save is not complete XML")


def _encrypt_save_bytes(xml: bytes) -> bytes:
    if not _is_complete_xml(xml):
        raise SaveFormatError("refusing to encrypt malformed save XML")
    padder = padding.PKCS7(_BLOCK_BITS).padder()
    padded = padder.update(xml) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_AES_KEY), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _remove_pkcs7(data: bytes) -> bytes:
    if not data:
        raise SaveFormatError("save has no PKCS7 padding")
    amount = data[-1]
    if amount < 1 or amount > 16 or data[-amount:] != bytes((amount,)) * amount:
        raise SaveFormatError("save has invalid PKCS7 padding")
    return data[:-amount]


def _is_complete_xml(data: bytes) -> bool:
    parser = expat.ParserCreate()
    depth = 0
    root_name: str | None = None

    def start(name: str, _attributes: dict[str, str]) -> None:
        nonlocal depth, root_name
        if depth == 0:
            root_name = name.rsplit("}", 1)[-1]
        depth += 1

    def end(_name: str) -> None:
        nonlocal depth
        depth -= 1

    def reject_external(*_args: object) -> int:
        raise SaveFormatError("external entities are not allowed in save XML")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.ExternalEntityRefHandler = reject_external
    try:
        parser.Parse(data, True)
    except (expat.ExpatError, SaveFormatError, ValueError):
        return False
    return root_name == "plist" and depth == 0


def _parse_xml(data: bytes) -> ET.Element:
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError) as exc:
        raise SaveFormatError("save contains malformed XML") from exc
    if _local_name(root) != "plist":
        raise SaveFormatError("save XML root is not plist")
    return root


def _extract_allowed_root_values(
    data: bytes, allowed: frozenset[str]
) -> dict[str, tuple[str, str]]:
    """Stream only allowlisted scalar values out of a manager save.

    In particular, this avoids constructing an XML tree containing password or
    token values from ``GJA_002``/``GJA_005``.
    """
    parser = expat.ParserCreate()
    stack: list[str] = []
    root_depth: int | None = None
    root_count = 0
    pending_key: str | None = None
    capturing_key = False
    key_text: list[str] = []
    capturing_value = False
    value_tag = ""
    value_text: list[str] = []
    values: dict[str, tuple[str, str]] = {}

    def start(name: str, _attributes: dict[str, str]) -> None:
        nonlocal root_depth, root_count, pending_key
        nonlocal capturing_key, key_text, capturing_value, value_tag, value_text
        local = name.rsplit("}", 1)[-1]
        if not stack:
            if local != "plist":
                raise SaveFormatError("save XML root is not plist")
            stack.append(local)
            return
        if len(stack) == 1:
            if local not in _DICT_TAGS:
                raise SaveFormatError("manager plist root value is not a dictionary")
            root_count += 1
            if root_count > 1:
                raise SaveFormatError("manager plist contains multiple root dictionaries")
            stack.append(local)
            root_depth = len(stack)
            return

        direct_root_child = root_depth is not None and len(stack) == root_depth
        if direct_root_child and local in _KEY_TAGS:
            if pending_key is not None:
                raise SaveFormatError("manager dictionary contains an unpaired key")
            capturing_key = True
            key_text = []
        elif direct_root_child:
            if pending_key is None:
                raise SaveFormatError("manager dictionary contains a value without a key")
            if pending_key in allowed:
                if local not in _STRING_TAGS | _INT_TAGS:
                    raise SaveFormatError(f"manager field {pending_key} has an invalid type")
                capturing_value = True
                value_tag = local
                value_text = []
        elif capturing_value:
            raise SaveFormatError("allowlisted manager scalar contains nested XML")
        stack.append(local)

    def text(content: str) -> None:
        if capturing_key:
            key_text.append(content)
        elif capturing_value:
            value_text.append(content)

    def end(name: str) -> None:
        nonlocal pending_key, capturing_key, capturing_value
        local = name.rsplit("}", 1)[-1]
        direct_root_child = root_depth is not None and len(stack) == root_depth + 1
        if direct_root_child and local in _KEY_TAGS and capturing_key:
            pending_key = "".join(key_text)
            capturing_key = False
        elif direct_root_child and local not in _KEY_TAGS:
            if pending_key in allowed:
                if pending_key in values:
                    raise SaveFormatError(f"save contains duplicate {pending_key} keys")
                values[pending_key] = (value_tag, "".join(value_text))
            pending_key = None
            capturing_value = False
        elif root_depth is not None and len(stack) == root_depth and local in _DICT_TAGS:
            if pending_key is not None:
                raise SaveFormatError("manager dictionary contains an unpaired key")
        stack.pop()

    def reject_external(*_args: object) -> int:
        raise SaveFormatError("external entities are not allowed in save XML")

    parser.StartElementHandler = start
    parser.CharacterDataHandler = text
    parser.EndElementHandler = end
    parser.ExternalEntityRefHandler = reject_external
    try:
        parser.Parse(data, True)
    except (expat.ExpatError, SaveFormatError) as exc:
        if isinstance(exc, SaveFormatError):
            raise
        raise SaveFormatError("save contains malformed XML") from exc
    if root_count != 1:
        raise SaveFormatError("manager plist has no root dictionary")
    return values


def _primitive_string(value: tuple[str, str] | None) -> str:
    if value is None or value[0] not in _STRING_TAGS:
        return ""
    return value[1]


def _primitive_int(value: tuple[str, str] | None, default: int) -> int:
    if value is None or value[0] not in _INT_TAGS | _STRING_TAGS:
        return default
    try:
        return int(value[1].strip())
    except ValueError:
        return default


def _local_name(element_or_name: ET.Element | str) -> str:
    name = element_or_name.tag if isinstance(element_or_name, ET.Element) else element_or_name
    return name.rsplit("}", 1)[-1]


def _root_dict(plist: ET.Element) -> ET.Element:
    dictionaries = [child for child in plist if _local_name(child) in _DICT_TAGS]
    if len(dictionaries) != 1:
        raise SaveFormatError("save plist must contain exactly one root dictionary")
    return dictionaries[0]


def _pairs(dictionary: ET.Element) -> Iterator[tuple[str, ET.Element]]:
    if _local_name(dictionary) not in _DICT_TAGS:
        raise SaveFormatError("expected a save dictionary")
    children = list(dictionary)
    if len(children) % 2:
        raise SaveFormatError("save dictionary contains an unpaired key")
    for position in range(0, len(children), 2):
        key_element = children[position]
        if _local_name(key_element) not in _KEY_TAGS:
            raise SaveFormatError("save dictionary contains a value without a key")
        key = "".join(key_element.itertext())
        yield key, children[position + 1]


def _selected_pairs(dictionary: ET.Element, selected: frozenset[str]) -> dict[str, ET.Element]:
    found: dict[str, ET.Element] = {}
    for key, value in _pairs(dictionary):
        if key not in selected:
            continue
        if key in found:
            raise SaveFormatError(f"save contains duplicate {key} keys")
        found[key] = value
    return found


def _local_level_array(root: ET.Element) -> ET.Element:
    values = _selected_pairs(root, frozenset(("LLM_01",)))
    local = values.get("LLM_01")
    if local is None or _local_name(local) not in _DICT_TAGS:
        raise SaveFormatError("save has no LLM_01 local-level array")
    marker = _selected_pairs(local, frozenset(("_isArr",))).get("_isArr")
    if not _as_bool(marker, False):
        raise SaveFormatError("LLM_01 is not marked as an array")
    return local


def _as_string(value: ET.Element | None) -> str:
    if value is None or _local_name(value) not in _STRING_TAGS:
        return ""
    return "".join(value.itertext())


def _as_int(value: ET.Element | None, default: int) -> int:
    if value is None or _local_name(value) not in _INT_TAGS | _STRING_TAGS:
        return default
    try:
        return int("".join(value.itertext()).strip())
    except ValueError:
        return default


def _as_bool(value: ET.Element | None, default: bool) -> bool:
    if value is None:
        return default
    tag = _local_name(value)
    if tag in _TRUE_TAGS:
        return True
    if tag in _FALSE_TAGS:
        return False
    return default


def _validated_compiled_level(compiled: CompiledLevel) -> dict:
    required = (
        "name",
        "description",
        "song_id",
        "custom_song_id",
        "level_string",
        "object_count",
    )
    missing = [field for field in required if not hasattr(compiled, field)]
    if missing:
        raise TypeError(f"compiled level is missing {', '.join(missing)}")

    name = compiled.name
    description = compiled.description
    level_string = compiled.level_string
    if not isinstance(name, str) or not name.strip():
        raise ValueError("compiled level name must be a non-empty string")
    if not isinstance(description, str):
        raise TypeError("compiled level description must be a string")
    if not isinstance(level_string, str) or not level_string:
        raise ValueError("compiled level string must be a non-empty string")
    _validate_xml_text(name, "level name")
    _validate_xml_text(description, "level description")

    numeric: dict[str, int] = {}
    for field in ("song_id", "custom_song_id", "object_count"):
        value = getattr(compiled, field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"compiled level {field} must be a non-negative integer")
        numeric[field] = value
    return {
        "name": name,
        "description": description,
        "level_string": level_string,
        **numeric,
    }


def _validate_xml_text(value: str, field: str) -> None:
    for character in value:
        number = ord(character)
        if not (
            number in (0x9, 0xA, 0xD)
            or 0x20 <= number <= 0xD7FF
            or 0xE000 <= number <= 0xFFFD
            or 0x10000 <= number <= 0x10FFFF
        ):
            raise ValueError(f"{field} contains a character XML cannot represent")


def _build_level_entry(
    *,
    index: int,
    name: str,
    description: str,
    song_id: int,
    custom_song_id: int,
    level_string: str,
    object_count: int,
    creator: str,
    user_id: int,
    account_id: int,
    binary_version: int,
) -> bytes:
    escaped_name = html.escape(name, quote=False)
    escaped_creator = html.escape(creator, quote=False)
    encoded_description = base64.b64encode(description.encode("utf-8")).decode("ascii")
    encoded_level = base64.urlsafe_b64encode(
        gzip.compress(level_string.encode("utf-8"), mtime=0)
    ).decode("ascii")

    properties = [
        "<k>kCEK</k><i>4</i>",
        f"<k>k2</k><s>{escaped_name}</s>",
        f"<k>k3</k><s>{encoded_description}</s>",
        f"<k>k4</k><s>{encoded_level}</s>",
        f"<k>k5</k><s>{escaped_creator}</s>",
        f"<k>k6</k><i>{user_id}</i>",
        f"<k>k8</k><i>{song_id}</i>",
        "<k>k13</k><t />",
        "<k>k21</k><i>2</i>",
        "<k>k16</k><i>1</i>",
        f"<k>k45</k><i>{custom_song_id}</i>",
        f"<k>k48</k><i>{object_count}</i>",
        f"<k>k60</k><i>{account_id}</i>",
    ]
    if binary_version > 0:
        properties.append(f"<k>k50</k><i>{binary_version}</i>")
    return f'<k>k_{index}</k><d>{"".join(properties)}</d>'.encode("utf-8")


def _llm_01_insert_offset(xml: bytes) -> int:
    """Locate LLM_01's closing tag without serializing any existing XML."""
    parser = expat.ParserCreate()
    stack: list[str] = []
    root_depth: int | None = None
    pending_root_key: str | None = None
    capturing_key = False
    key_text: list[str] = []
    target_depth: int | None = None
    offsets: list[int] = []

    def start(name: str, _attributes: dict[str, str]) -> None:
        nonlocal root_depth, capturing_key, key_text, target_depth
        local = name.rsplit("}", 1)[-1]
        if root_depth is None and local in _DICT_TAGS and len(stack) == 1 and stack[0] == "plist":
            stack.append(local)
            root_depth = len(stack)
            return

        direct_root_child = root_depth is not None and len(stack) == root_depth
        if direct_root_child and local in _KEY_TAGS:
            capturing_key = True
            key_text = []
        elif direct_root_child and pending_root_key == "LLM_01" and local in _DICT_TAGS:
            target_depth = len(stack) + 1
        stack.append(local)

    def text(data: str) -> None:
        if capturing_key:
            key_text.append(data)

    def end(name: str) -> None:
        nonlocal capturing_key, pending_root_key, target_depth
        local = name.rsplit("}", 1)[-1]
        if target_depth is not None and len(stack) == target_depth and local in _DICT_TAGS:
            offsets.append(parser.CurrentByteIndex)
            target_depth = None

        direct_root_child = root_depth is not None and len(stack) == root_depth + 1
        if direct_root_child and local in _KEY_TAGS and capturing_key:
            pending_root_key = "".join(key_text)
            capturing_key = False
        elif direct_root_child and local not in _KEY_TAGS:
            pending_root_key = None
        stack.pop()

    def reject_external(*_args: object) -> int:
        raise SaveFormatError("external entities are not allowed in save XML")

    parser.StartElementHandler = start
    parser.CharacterDataHandler = text
    parser.EndElementHandler = end
    parser.ExternalEntityRefHandler = reject_external
    try:
        parser.Parse(xml, True)
    except (expat.ExpatError, SaveFormatError) as exc:
        if isinstance(exc, SaveFormatError):
            raise
        raise SaveFormatError("save contains malformed XML") from exc
    if len(offsets) != 1:
        raise SaveFormatError("save must contain exactly one root LLM_01 dictionary")
    return offsets[0]


def _read_regular_file(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SaveError(f"could not safely open {path}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SaveError(f"save path is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), file_stat
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _exclusive_import_lock(path: Path) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ConcurrentImportError("could not safely open the GDAITRANS import lock") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ConcurrentImportError("the GDAITRANS import lock is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConcurrentImportError("another GDAITRANS import is already in progress") from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _assert_game_stopped() -> None:
    commands = (
        ("/usr/bin/pgrep", "-x", "Geometry Dash"),
        (
            "/usr/bin/pgrep",
            "-f",
            r"/Geometry Dash\.app/Contents/MacOS/Geometry Dash($| )",
        ),
    )
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise SaveError("could not verify that Geometry Dash is stopped") from exc
        if completed.returncode == 0:
            raise GameRunningError(
                "Geometry Dash is running; close it completely before importing a level"
            )
        if completed.returncode != 1:
            raise SaveError("could not verify that Geometry Dash is stopped")


def _write_backup(directory: Path, data: bytes, digest: bytes) -> Path:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise SaveError(f"could not create backup directory {directory}") from exc
    if not directory.is_dir():
        raise SaveError(f"backup path is not a directory: {directory}")

    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    stem = f"CCLocalLevels.{timestamp}.{digest.hex()[:12]}"
    for counter in range(1000):
        suffix = "" if counter == 0 else f".{counter}"
        path = directory / f"{stem}{suffix}.dat"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            raise SaveError(f"could not create encrypted backup in {directory}") from exc
        try:
            _write_all(descriptor, data)
            _sync_file(descriptor)
        except Exception:
            os.close(descriptor)
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        else:
            os.close(descriptor)
        _sync_directory(directory)
        return path
    raise SaveError("could not allocate a unique backup filename")


def _write_atomic_replacement(
    *, save_path: Path, data: bytes, mode: int, original_hash: bytes
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".CCLocalLevels.", suffix=".tmp", dir=save_path.parent
    )
    temporary = Path(temporary_name)
    replaced = False
    try:
        os.fchmod(descriptor, mode)
        _write_all(descriptor, data)
        _sync_file(descriptor)
        os.close(descriptor)
        descriptor = -1

        # These are deliberately the final operations before os.replace.  The
        # game guard and content hash fail closed if either actor raced us.
        _assert_game_stopped()
        _assert_unchanged(save_path, original_hash)
        os.replace(temporary, save_path)
        replaced = True
        _sync_directory(save_path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not replaced:
            with contextlib.suppress(OSError):
                temporary.unlink()


def _assert_unchanged(path: Path, expected_hash: bytes) -> None:
    current, _ = _read_regular_file(path)
    if hashlib.sha256(current).digest() != expected_hash:
        raise StaleSaveError(
            "CCLocalLevels.dat changed during import; no replacement was made"
        )


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise SaveError("a save write did not make progress")
        view = view[written:]


def _sync_file(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
        full_sync = getattr(fcntl, "F_FULLFSYNC", None)
        if full_sync is not None:
            fcntl.fcntl(descriptor, full_sync)
    except OSError as exc:
        raise SaveError("could not durably sync save data") from exc


def _sync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise SaveError(f"could not durably sync directory {directory}") from exc
