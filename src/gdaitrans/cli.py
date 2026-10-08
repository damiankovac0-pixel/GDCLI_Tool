"""JSON-output authoring and background game control for AI clients."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from . import bridge, catalog, examples, level, macos, saves


def _read_json(path: str):
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).expanduser().open(encoding="utf-8") as file:
        return json.load(file)


def doctor() -> dict:
    result = {"platform": sys.platform, "model_api_required": False,
              "network_server": False, "supported_platform": sys.platform == "darwin"}
    if sys.platform != "darwin":
        result["error"] = "native game integration currently supports macOS only; compilation/export is portable"
        return result
    result["game"] = macos.status()
    try:
        result["account"] = saves.account_info()
        result["local_level_count"] = len(saves.list_levels())
    except (OSError, saves.SaveError) as error:
        result["save_error"] = str(error)
    try:
        result["bridge"] = bridge.status()
    except bridge.BridgeError as error:
        result["bridge_error"] = str(error)
    return result


def open_game(timeout: float = 30.0) -> dict:
    """Start without activation, then wait for the actual loaded bridge."""
    native = macos.open_game(timeout)
    deadline = time.monotonic() + timeout
    last_error = "bridge not ready"
    while time.monotonic() < deadline:
        try:
            current = bridge.rpc("status", timeout=2.0)
            if current.get("ready") is True:
                return {"game": native, "bridge": current}
        except bridge.BridgeError as error:
            last_error = str(error)
        time.sleep(0.1)
    raise bridge.BridgeUnavailableError(f"Game launched but its bridge is unavailable: {last_error}. Run gdcli install first.")


def create(spec: dict, *, open_mode: str | None = None) -> dict:
    if open_mode not in (None, "editor", "play"):
        raise ValueError("open_mode must be editor, play or omitted")
    compiled = level.compile_level(spec)
    open_game()
    result = bridge.create_level(compiled)
    if open_mode is not None:
        result["scene"] = bridge.open_level(compiled.name, open_mode)
    return result


def update(spec: dict, *, open_mode: str | None = None) -> dict:
    if open_mode not in (None, "editor", "play"):
        raise ValueError("open_mode must be editor, play or omitted")
    compiled = level.compile_level(spec)
    open_game()
    result = bridge.update_level(compiled)
    if open_mode is not None:
        result["scene"] = bridge.open_level(compiled.name, open_mode)
    return result


def capture_game(destination: str | Path | None = None) -> dict:
    result = bridge.capture()
    if destination is not None:
        path = Path(destination).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path != Path(result["image"]):
            shutil.copyfile(result["image"], path)
        result = {**result, "image": str(path)}
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gdcli", description="Chat-driven GD authoring; background controls never use desktop input.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Inspect installed game, local bridge, and non-secret account identity")
    objects = commands.add_parser("objects", help="Search the bundled object catalog")
    objects.add_argument("query")
    objects.add_argument("--limit", type=int, default=20)
    validate = commands.add_parser("validate", help="Check a JSON level specification")
    validate.add_argument("spec", help="JSON path, or - for stdin")
    compile_parser = commands.add_parser("compile", help="Export a specification as .gmd")
    compile_parser.add_argument("spec")
    compile_parser.add_argument("--output", default="outputs/level.gmd")
    creation = commands.add_parser("create", help="Create a new account-owned level in the running game with a save backup")
    creation.add_argument("spec")
    creation.add_argument("--open", dest="open_mode", choices=("editor", "play"))
    revision = commands.add_parser("update", help="Revise only an original tool-created level in the current account, retaining a backup")
    revision.add_argument("spec")
    revision.add_argument("--open", dest="open_mode", choices=("editor", "play"))
    demo = commands.add_parser("demo", help="Generate an original neon pad-run example, not a copied community level")
    demo.add_argument("--name", default="Chat Neon Run")
    demo.add_argument("--output", default="outputs/neon-run.json")
    demo.add_argument("--create", action="store_true")
    demo.add_argument("--open", dest="open_mode", choices=("editor", "play"))
    offline = commands.add_parser("import-save", help="Explicit offline import; refuses live save writes")
    offline.add_argument("spec")
    offline.add_argument("--save-dir", type=Path)
    offline.add_argument("--backup-dir", type=Path)
    offline.add_argument("--allow-local", action="store_true")
    listing = commands.add_parser("levels", help="List saved local editor levels")
    listing.add_argument("--save-dir", type=Path)
    install = commands.add_parser("install", help="Install the official Geode loader and this local bridge; game must be closed")
    install.add_argument("--bridge", type=Path, required=True, help="Built/downloaded .geode package")
    install.add_argument("--package", type=Path, help="Optional local official Geode v5.10.1 .pkg; otherwise downloaded and SHA-256 checked")
    commands.add_parser("uninstall", help="Remove our managed bridge/loader and restore the backed-up game library")
    game = commands.add_parser("game", help="Background launch, actual scene capture, and isolated in-game controls")
    game_commands = game.add_subparsers(dest="game_command", required=True)
    game_commands.add_parser("status")
    game_commands.add_parser("open")
    game_commands.add_parser("close", help="Normal quit only; never force-kill")
    game_commands.add_parser("leave", help="Save/exit the editor or quit play normally, without desktop input")
    opening = game_commands.add_parser("level", help="Open an exact local level without desktop activation")
    opening.add_argument("name")
    opening.add_argument("--mode", choices=("editor", "play"), default="editor")
    capture = game_commands.add_parser("capture")
    capture.add_argument("--output", help="Optional PNG copy; otherwise retain the private native capture")
    click = game_commands.add_parser("click", help="Activate a Cocos menu item at normalized top-left coordinates")
    click.add_argument("x", type=float)
    click.add_argument("y", type=float)
    key = game_commands.add_parser("key")
    key.add_argument("key")
    key.add_argument("--hold", type=float, default=0.08)
    play = game_commands.add_parser("play", help="Run isolated timed game inputs and return true game state plus a frame")
    play.add_argument("--seconds", type=float, required=True)
    play.add_argument("--inputs", help="JSON array of {at,key,action}; none means observe normal gameplay")
    commands.add_parser("serve", help="Serve tools/images over private MCP stdio, with no network port")
    return parser


def _execute(args: argparse.Namespace):
    if args.command == "doctor":
        return doctor()
    if args.command == "objects":
        return catalog.find_objects(args.query, args.limit)
    if args.command in ("validate", "compile"):
        compiled = level.compile_level(_read_json(args.spec))
        result = {key: value for key, value in asdict(compiled).items() if key != "level_string"}
        result["level_string_bytes"] = len(compiled.level_string.encode("utf-8"))
        if args.command == "compile":
            result["artifact"] = str(level.export_gmd(compiled, args.output).resolve())
        return result
    if args.command == "create":
        return create(_read_json(args.spec), open_mode=args.open_mode)
    if args.command == "update":
        return update(_read_json(args.spec), open_mode=args.open_mode)
    if args.command == "demo":
        if args.open_mode and not args.create:
            raise ValueError("demo --open requires --create")
        spec = examples.neon_run(args.name)
        path = Path(args.output).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        compiled = level.compile_level(spec)
        result = {"spec": str(path), "name": compiled.name, "object_count": compiled.object_count}
        if args.create:
            result["created"] = create(spec, open_mode=args.open_mode)
        return result
    if args.command == "import-save":
        return saves.import_level(level.compile_level(_read_json(args.spec)), args.save_dir, args.backup_dir, args.allow_local)
    if args.command == "levels":
        return saves.list_levels(args.save_dir)
    if args.command in ("install", "uninstall"):
        from . import install
        return install.install(args.bridge, args.package) if args.command == "install" else install.uninstall()
    if args.command == "serve":
        from .server import serve
        serve()
        return None
    if args.game_command == "status":
        return {"game": macos.status(), "bridge": bridge.status()}
    if args.game_command == "open":
        return open_game()
    if args.game_command == "close":
        return macos.close_game()
    if args.game_command == "leave":
        return bridge.leave_level()
    if args.game_command == "level":
        open_game()
        return bridge.open_level(args.name, args.mode)
    if args.game_command == "capture":
        return capture_game(args.output)
    if args.game_command == "click":
        return bridge.click(args.x, args.y)
    if args.game_command == "key":
        return bridge.press(args.key, args.hold)
    if args.game_command == "play":
        return bridge.run_inputs(_read_json(args.inputs) if args.inputs else [], args.seconds)
    raise RuntimeError("unhandled command")


def main() -> None:
    args = _parser().parse_args()
    try:
        result = _execute(args)
        if result is not None:
            print(json.dumps(result, indent=2, ensure_ascii=False))
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted"}), file=sys.stderr)
        raise SystemExit(130) from None
