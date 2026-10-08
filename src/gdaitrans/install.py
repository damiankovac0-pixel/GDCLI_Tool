"""Install the pinned official Geode payload without sudo or global hooks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.request import urlopen
import zipfile

from . import macos, saves

GEODE_VERSION = "5.10.1"
MOD_ID = "pandagamer.gdcli"
PACKAGE_URL = f"https://github.com/geode-sdk/geode/releases/download/v{GEODE_VERSION}/geode-installer-v{GEODE_VERSION}-mac.pkg"
PACKAGE_SHA256 = "7fa0f3d91e568c373f8920506bf623cc8ad22fcac18114fafc6d739139a87139"


def _root() -> Path:
    root = Path.home() / "Library/Application Support/GDAITRANS"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise RuntimeError("installation state must be a private user-owned directory")
    root.chmod(0o700)
    return root


def _digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _copy_atomic(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".gdcli-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_mod(package: Path) -> None:
    if package.suffix != ".geode" or not package.is_file():
        raise ValueError("--bridge must be a built .geode package")
    with zipfile.ZipFile(package) as archive:
        metadata = json.loads(archive.read("mod.json"))
        if metadata.get("id") != MOD_ID:
            raise ValueError(f"expected the local bridge {MOD_ID}, not another mod")
        if not any(name.endswith(".dylib") for name in archive.namelist()):
            raise ValueError("bridge package has no macOS native library")


def _official_payload(package: Path | None, temporary: Path) -> Path:
    if package is None:
        package = temporary / "geode.pkg"
        with urlopen(PACKAGE_URL, timeout=60) as response, package.open("wb") as output:
            shutil.copyfileobj(response, output)
    package = package.expanduser().resolve()
    if _digest(package) != PACKAGE_SHA256:
        raise ValueError("Geode installer SHA-256 does not match the pinned official GitHub release")
    expanded = temporary / "expanded"
    # Expand only; never execute the package's privileged interactive postinstall.
    subprocess.run(["pkgutil", "--expand-full", str(package), str(expanded)], check=True)
    payload = expanded / "Payload"
    for name in ("libfmod.dylib", "Geode.dylib", "GeodeBootstrapper.dylib"):
        if not (payload / name).is_file():
            raise RuntimeError(f"official installer is missing {name}")
    return payload


def _snapshot_saves(destination: Path) -> list[str]:
    copied = []
    for name in ("CCLocalLevels.dat", "CCGameManager.dat"):
        source = saves.default_save_dir() / name
        if source.is_file():
            target = destination / name
            _copy_atomic(source, target, 0o600)
            copied.append(str(target))
    return copied


def install(bridge_package: Path, official_package: Path | None = None) -> dict:
    """Back up originals, then install only the approved game-local loader/mod."""
    saves._assert_game_stopped()
    bridge_package = bridge_package.expanduser().resolve()
    _validate_mod(bridge_package)
    app = macos.app_path().resolve()
    frameworks = app / "Contents/Frameworks"
    geode = app / "Contents/geode"
    fmod = frameworks / "libfmod.dylib"
    if not fmod.is_file():
        raise RuntimeError("the game is missing its original FMOD library")
    root = _root()
    manifest_path = root / "installation.json"
    mod_destination = geode / "mods" / f"{MOD_ID}.geode"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["app"] != str(app):
            raise RuntimeError("our managed installation belongs to a different game application")
        previous = next(item for item in manifest["files"] if item["path"] == str(mod_destination))
        if mod_destination.exists() and _digest(mod_destination) != previous["sha256"]:
            raise RuntimeError("the installed bridge changed outside this tool; refusing to overwrite it")
        _copy_atomic(bridge_package, mod_destination, 0o644)
        previous["sha256"] = _digest(mod_destination)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return {"installed": True, "bridge": str(mod_destination), "backup": manifest["backup"], "updated": True}

    loader_present = (frameworks / "Geode.dylib").is_file()
    if not loader_present and (frameworks / "restore_fmod.dylib").exists():
        raise RuntimeError("an incomplete existing loader installation needs manual recovery first")
    if mod_destination.exists():
        raise RuntimeError("this bridge is already installed outside our managed installer")
    if not loader_present and (geode / "resources/geode.loader").exists():
        raise RuntimeError("existing Geode resources need manual review before installing a loader")
    backup = Path(tempfile.mkdtemp(prefix="installation-", dir=root))
    _copy_atomic(app / "Contents/MacOS/Geometry Dash", backup / "Geometry Dash", 0o600)
    _copy_atomic(fmod, backup / "libfmod.dylib", 0o600)
    save_backups = _snapshot_saves(backup)
    manifest = {"app": str(app), "backup": str(backup), "files": [], "resources": None}
    try:
        with tempfile.TemporaryDirectory(prefix="gdcli-geode-") as name:
            if not loader_present:
                payload = _official_payload(official_package, Path(name))
                for library in ("restore_fmod.dylib", "Geode.dylib", "GeodeBootstrapper.dylib", "libfmod.dylib"):
                    destination = frameworks / library
                    source = backup / "libfmod.dylib" if library == "restore_fmod.dylib" else payload / library
                    record = {"path": str(destination), "restore": str(backup / "libfmod.dylib") if library == "libfmod.dylib" else None}
                    manifest["files"].append(record)
                    _copy_atomic(source, destination, 0o755)
                    record["sha256"] = _digest(destination)
                resources = geode / "resources/geode.loader"
                manifest["resources"] = str(resources)
                shutil.copytree(payload / "resources", resources)
            record = {"path": str(mod_destination), "restore": None}
            manifest["files"].append(record)
            _copy_atomic(bridge_package, mod_destination, 0o644)
            record["sha256"] = _digest(mod_destination)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        manifest_path.chmod(0o600)
    except BaseException:
        # Restore our own changes; keep the exact backups even if installation fails.
        for item in reversed(manifest["files"]):
            destination = Path(item["path"])
            if item["restore"]:
                _copy_atomic(Path(item["restore"]), destination, 0o755)
            else:
                destination.unlink(missing_ok=True)
        if manifest["resources"]:
            shutil.rmtree(manifest["resources"], ignore_errors=True)
        raise
    return {"installed": True, "bridge": str(mod_destination), "loader_installed": not loader_present,
            "backup": str(backup), "save_backups": save_backups, "sudo_required": False}


def uninstall() -> dict:
    """Restore managed game files; never restore/replace the user's current saves."""
    saves._assert_game_stopped()
    manifest_path = _root() / "installation.json"
    if not manifest_path.exists():
        raise RuntimeError("there is no installation managed by this tool")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["files"]:
        path = Path(item["path"])
        if path.exists() and _digest(path) != item["sha256"]:
            raise RuntimeError(f"managed file changed outside this tool; refusing to remove it: {path}")
    for item in reversed(manifest["files"]):
        path = Path(item["path"])
        if item["restore"]:
            _copy_atomic(Path(item["restore"]), path, 0o755)
        else:
            path.unlink(missing_ok=True)
    if manifest["resources"]:
        shutil.rmtree(manifest["resources"])
    manifest_path.unlink()
    return {"uninstalled": True, "original_library_restored": any(item["restore"] for item in manifest["files"]),
            "backup_retained": manifest["backup"], "current_levels_unchanged": True}
