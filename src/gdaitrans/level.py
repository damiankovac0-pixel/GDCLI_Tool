"""Compile authoring specifications to Geometry Dash level strings and GMD files.

The level-string and plist keys follow the formats documented by gd.docs:
https://github.com/gd-programming/gd.docs/tree/main/docs/resources/client
"""

from __future__ import annotations

import base64
import gzip
import io
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_PROPERTY_KEY = re.compile(r"[1-9][0-9]*\Z")
_SETTING_KEY = re.compile(r"k[AS][0-9]+\Z")
_MAX_GD_ID = 2_147_483_647

# The color table is adapted from GMDKit's MIT-licensed LEVEL_DEFAULT; its
# bundled license is in data/GMDKIT_LICENSE.  The gameplay keys follow
# gd.docs: kA2=0 is cube, kA4=0 is normal speed, and kA22=0 is classic mode.
_DEFAULT_COLOR_CHANNELS = (
    "1_40_2_125_3_255_11_255_12_255_13_255_4_-1_6_1000_7_1_15_1_18_0_8_1|"
    "1_0_2_102_3_255_11_255_12_255_13_255_4_-1_6_1001_7_1_15_1_18_0_8_1|"
    "1_255_2_255_3_255_11_255_12_255_13_255_4_-1_6_1002_5_1_7_1_15_1_18_0_8_1|"
    "1_255_2_255_3_255_11_255_12_255_13_255_4_-1_6_1004_7_1_15_1_18_0_8_1|"
)
_DEFAULT_SETTINGS: tuple[tuple[str, Any], ...] = (
    ("kS38", _DEFAULT_COLOR_CHANNELS),
    ("kA1", 0),
    ("kA2", 0),
    ("kA3", False),
    ("kA4", 0),
    ("kA6", 0),
    ("kA7", 0),
    ("kA8", False),
    ("kA9", False),
    ("kA10", False),
    ("kA11", False),
    ("kA13", 0),
    ("kA14", ""),
    ("kA15", False),
    ("kA16", False),
    ("kA17", 0),
    ("kA18", 0),
    ("kA22", False),
    ("kA25", 0),
    ("kA27", True),
    ("kA31", True),
    ("kA32", True),
    ("kA33", True),
    ("kA34", True),
    ("kA37", True),
    ("kA38", True),
    ("kA39", True),
    ("kA40", True),
    ("kA41", True),
    ("kA42", True),
)


@dataclass(frozen=True)
class CompiledLevel:
    """A validated level ready for native-save or ``.gmd`` export."""

    name: str
    description: str
    song_id: int
    custom_song_id: int
    level_string: str
    object_count: int


def _require_dict(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{path} must be an object")
    return value


def _require_nonnegative_id(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{path} must be an integer")
    if value < 0 or value > _MAX_GD_ID:
        raise ValueError(f"{path} must be between 0 and {_MAX_GD_ID}")
    return value


def _require_object_id(value: object, path: str) -> int:
    object_id = _require_nonnegative_id(value, path)
    if object_id == 0:
        raise ValueError(f"{path} must be greater than 0 (0 is the level-start sentinel)")
    return object_id


def _require_number(value: object, path: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{path} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} must be finite")
    return value


def _validate_xml_text(value: str, path: str) -> str:
    for character in value:
        codepoint = ord(character)
        if not (
            codepoint in (0x09, 0x0A, 0x0D)
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        ):
            raise ValueError(
                f"{path} contains a character not permitted by XML 1.0 "
                f"(U+{codepoint:04X})"
            )
    return value


def _format_primitive(value: object, path: str) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        rendered = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must be finite")
        rendered = repr(value)
    elif isinstance(value, str):
        rendered = value
    else:
        raise TypeError(f"{path} must be a string, number, or boolean")

    forbidden = sorted({character for character in rendered if character in ",;\r\n\x00"})
    if forbidden:
        escaped = ", ".join(repr(character) for character in forbidden)
        raise ValueError(f"{path} contains unsafe level-string delimiter(s): {escaped}")
    return rendered


def _reject_unknown_keys(value: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(
        (key for key in value if not isinstance(key, str) or key not in allowed),
        key=repr,
    )
    if unknown:
        rendered = ", ".join(repr(key) for key in unknown)
        raise ValueError(f"{path} contains unknown field(s): {rendered}")


def compile_level(spec: dict) -> CompiledLevel:
    """Validate and compile a JSON-compatible authoring specification.

    Object order is retained.  Raw numeric object properties are emitted in
    numeric-key order so equivalent JSON objects compile identically.
    """

    spec = _require_dict(spec, "spec")
    _reject_unknown_keys(
        spec,
        {"name", "description", "song_id", "custom_song_id", "settings", "objects"},
        "spec",
    )

    if "name" not in spec:
        raise ValueError("spec.name is required")
    name = spec["name"]
    if not isinstance(name, str):
        raise TypeError("spec.name must be a string")
    if not name.strip():
        raise ValueError("spec.name must not be empty")
    _validate_xml_text(name, "spec.name")

    description = spec.get("description", "")
    if not isinstance(description, str):
        raise TypeError("spec.description must be a string")
    _validate_xml_text(description, "spec.description")

    song_id = _require_nonnegative_id(spec.get("song_id", 0), "spec.song_id")
    custom_song_id = _require_nonnegative_id(
        spec.get("custom_song_id", 0), "spec.custom_song_id"
    )
    if song_id and custom_song_id:
        raise ValueError("spec.song_id and spec.custom_song_id cannot both be non-zero")

    raw_settings = _require_dict(spec.get("settings", {}), "spec.settings")
    settings: dict[str, object] = dict(_DEFAULT_SETTINGS)
    settings["kA1"] = song_id
    extra_setting_keys: list[str] = []
    for key, value in raw_settings.items():
        if not isinstance(key, str) or _SETTING_KEY.fullmatch(key) is None:
            raise ValueError(
                f"spec.settings key {key!r} must be a GD start key such as 'kA4' or 'kS38'"
            )
        _format_primitive(value, f"spec.settings[{key!r}]")
        if key not in settings:
            extra_setting_keys.append(key)
        settings[key] = value

    objects = spec.get("objects")
    if objects is None:
        raise ValueError("spec.objects is required")
    if not isinstance(objects, list):
        raise TypeError("spec.objects must be an array")

    setting_parts: list[str] = []
    for key, _ in _DEFAULT_SETTINGS:
        setting_parts.extend((key, _format_primitive(settings[key], f"spec.settings[{key!r}]")))
    for key in sorted(extra_setting_keys):
        setting_parts.extend((key, _format_primitive(settings[key], f"spec.settings[{key!r}]")))

    object_strings: list[str] = []
    for index, raw_object in enumerate(objects):
        path = f"spec.objects[{index}]"
        obj = _require_dict(raw_object, path)
        _reject_unknown_keys(obj, {"id", "x", "y", "properties"}, path)
        for required in ("id", "x", "y"):
            if required not in obj:
                raise ValueError(f"{path}.{required} is required")

        object_id = _require_object_id(obj["id"], f"{path}.id")
        x = _require_number(obj["x"], f"{path}.x")
        y = _require_number(obj["y"], f"{path}.y")
        properties = _require_dict(obj.get("properties", {}), f"{path}.properties")

        normalized_properties: list[tuple[int, str, str]] = []
        for key, value in properties.items():
            if not isinstance(key, str) or _PROPERTY_KEY.fullmatch(key) is None:
                raise ValueError(
                    f"{path}.properties key {key!r} must be a positive numeric GD property key"
                )
            numeric_key = int(key)
            if numeric_key > _MAX_GD_ID:
                raise ValueError(
                    f"{path}.properties key {key!r} exceeds the largest usable GD key"
                )
            if numeric_key in (1, 2, 3):
                field = {1: "id", 2: "x", 3: "y"}[numeric_key]
                raise ValueError(
                    f"{path}.properties[{key!r}] conflicts with typed field {path}.{field}"
                )
            normalized_properties.append(
                (numeric_key, key, _format_primitive(value, f"{path}.properties[{key!r}]"))
            )

        parts = [
            "1",
            str(object_id),
            "2",
            _format_primitive(x, f"{path}.x"),
            "3",
            _format_primitive(y, f"{path}.y"),
        ]
        for _, key, rendered in sorted(normalized_properties):
            parts.extend((key, rendered))
        object_strings.append(",".join(parts))

    level_string = ",".join(setting_parts) + ";"
    if object_strings:
        level_string += ";".join(object_strings) + ";"

    return CompiledLevel(
        name=name,
        description=description,
        song_id=song_id,
        custom_song_id=custom_song_id,
        level_string=level_string,
        object_count=len(objects),
    )


def _compressed_level_string(level_string: str) -> str:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0, compresslevel=9) as file:
        file.write(level_string.encode("utf-8"))
    return base64.urlsafe_b64encode(output.getvalue()).decode("ascii")


def _append_value(parent: ET.Element, key: str, tag: str, value: str | None = None) -> None:
    ET.SubElement(parent, "k").text = key
    node = ET.SubElement(parent, tag)
    if value is not None:
        node.text = value


def _safe_filename(name: str) -> str:
    filename = "".join(
        "_" if character in "/\\:\x00" or ord(character) < 0x20 else character
        for character in name
    ).strip(" .")
    return filename or "level"


def export_gmd(compiled: CompiledLevel, destination: str | Path) -> Path:
    """Write a deterministic legacy GDShare ``.gmd`` plist container."""

    if not isinstance(compiled, CompiledLevel):
        raise TypeError("compiled must be a CompiledLevel")
    _validate_xml_text(compiled.name, "compiled.name")
    _validate_xml_text(compiled.description, "compiled.description")
    song_id = _require_nonnegative_id(compiled.song_id, "compiled.song_id")
    custom_song_id = _require_nonnegative_id(
        compiled.custom_song_id, "compiled.custom_song_id"
    )
    if song_id and custom_song_id:
        raise ValueError("compiled.song_id and compiled.custom_song_id cannot both be non-zero")
    if not isinstance(compiled.level_string, str) or not compiled.level_string:
        raise ValueError("compiled.level_string must be a non-empty string")
    object_count = _require_nonnegative_id(compiled.object_count, "compiled.object_count")

    path = Path(destination).expanduser()
    if path.exists() and path.is_dir():
        path = path / f"{_safe_filename(compiled.name)}.gmd"
    elif path.suffix == "":
        path = path.with_suffix(".gmd")
    elif path.suffix.casefold() != ".gmd":
        raise ValueError("destination must be a .gmd file or a directory")
    path.parent.mkdir(parents=True, exist_ok=True)

    plist = ET.Element("plist", version="1.0", gjver="2.0")
    root = ET.SubElement(plist, "dict")
    _append_value(root, "kCEK", "i", "4")
    _append_value(root, "k2", "s", compiled.name)
    _append_value(
        root,
        "k3",
        "s",
        base64.urlsafe_b64encode(compiled.description.encode("utf-8")).decode("ascii"),
    )
    _append_value(root, "k4", "s", _compressed_level_string(compiled.level_string))
    _append_value(root, "k8", "i", str(song_id))
    _append_value(root, "k45", "i", str(custom_song_id))
    _append_value(root, "k13", "t")
    _append_value(root, "k21", "i", "2")
    _append_value(root, "k16", "i", "1")
    _append_value(root, "k17", "i", "22")
    _append_value(root, "k50", "i", "45")
    _append_value(root, "k47", "t")
    _append_value(root, "k48", "i", str(object_count))

    xml = ET.tostring(plist, encoding="utf-8", xml_declaration=True, short_empty_elements=True)
    path.write_bytes(xml)
    return path
