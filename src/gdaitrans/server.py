"""Expose the working local CLI operations to MCP-capable AI clients."""

from __future__ import annotations

from dataclasses import asdict
from functools import wraps
import json
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import bridge, catalog, level, macos, saves
from .cli import capture_game, create, doctor, open_game, update


def _tool_errors(function):
    """Report anticipated domain failures to the AI without pretending success."""
    @wraps(function)
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            raise ToolError(str(error)) from error
    return run


def make_server() -> MCPServer:
    server = MCPServer(
        "gdcli",
        instructions=(
            "Local Geometry Dash authoring and game control. The connected AI designs JSON object data; "
            "no model API or paid service is required. Run doctor first. Ask the user to sign in inside "
            "the game if account attribution is required; never ask for their password or token. "
            "Create uses the signed-in game's private Unix-socket bridge and retains an exact save backup. "
            "Game controls never use desktop mouse/keyboard or activate the application. "
            "Inspect actual scene captures and game-reported completion/deaths; compilation or elapsed "
            "time does not prove playability. Preserve existing editor work before scene transitions. "
            "Do not upload levels or alter unrelated game settings without a user request."
        ),
    )

    @server.tool(name="doctor")
    @_tool_errors
    def inspect_environment() -> dict:
        """Check the installed game, permissions, and allowlisted non-secret account identity."""
        return doctor()

    @server.tool()
    @_tool_errors
    def account_status() -> dict:
        """Read the saved Geometry Dash username/account ID, never authentication fields."""
        return saves.account_info()

    @server.tool(name="find_objects")
    @_tool_errors
    def search_objects(query: str, limit: int = 20) -> list[dict]:
        """Search actual GD object IDs/names/aliases in the bundled catalog."""
        return catalog.find_objects(query, limit)

    @server.tool(name="list_levels")
    @_tool_errors
    def inspect_local_levels() -> list[dict]:
        """List local editor levels and safe creator/object-count metadata."""
        return saves.list_levels()

    @server.tool()
    @_tool_errors
    def validate_level(spec: dict) -> dict:
        """Compile {name,description?,song_id?,custom_song_id?,settings?,objects:[{id,x,y,properties?}]}.
    
        settings uses actual GD start keys such as kA4 or kS38. Object properties
        are numeric GD key strings. This checks format validity, not playability.
        """
        compiled = level.compile_level(spec)
        return {key: value for key, value in asdict(compiled).items() if key != "level_string"}

    @server.tool()
    @_tool_errors
    def export_level(spec: dict, destination: str = "outputs/level.gmd") -> dict:
        """Compile an original structured level and export a GDShare-compatible .gmd artifact."""
        compiled = level.compile_level(spec)
        return {"name": compiled.name, "object_count": compiled.object_count,
                "artifact": str(level.export_gmd(compiled, destination).resolve())}

    @server.tool()
    @_tool_errors
    def create_level(spec: dict, open_mode: str | None = None) -> dict:
        """Create a new original account-owned local level, with an encrypted save backup.
    
        Duplicate names are refused. Optional open_mode is editor or play.
        No level is uploaded and no verification/completion flags are forged.
        """
        return create(spec, open_mode=open_mode)

    @server.tool()
    @_tool_errors
    def update_level(spec: dict, open_mode: str | None = None) -> dict:
        """Revise a previously tool-created level in this account, with a fresh exact save backup.
    
        The spec name identifies the level; existing unrelated levels cannot be updated.
        Optional open_mode is editor or play. Save/exit the editor before changing scenes.
        """
        return update(spec, open_mode=open_mode)

    @server.tool(name="open_game")
    @_tool_errors
    def launch_game() -> dict:
        """Start GD in the background and wait for the loaded in-game bridge."""
        return open_game()

    @server.tool()
    @_tool_errors
    def open_level(name: str, mode: str = "editor") -> dict:
        """Open an exact local level in the real editor or normal play, without stealing focus."""
        open_game()
        return bridge.open_level(name, mode)

    @server.tool()
    @_tool_errors
    def leave_level() -> dict:
        """Save/exit the current editor or quit play normally before creating, revising or opening a level."""
        return bridge.leave_level()

    @server.tool()
    @_tool_errors
    def game_state() -> dict:
        """Read actual account identity, scene, progress, deaths and completion from the engine."""
        return bridge.status()

    @server.tool(name="close_game")
    @_tool_errors
    def quit_game() -> dict:
        """Ask Geometry Dash to quit normally; never force-kill or dismiss unsaved-edit dialogs."""
        return macos.close_game()

    @server.tool(name="capture_game", structured_output=False)
    @_tool_errors
    def view_game(destination: str | None = None):
        """Capture the actual Cocos game scene and return image content, with no desktop capture."""
        result = capture_game(destination)
        return [json.dumps(result), Image(path=Path(result["image"]))]

    @server.tool(name="click_game")
    @_tool_errors
    def click_in_game(x: float, y: float) -> dict:
        """Activate a real Cocos menu item at normalized top-left coordinates; no OS cursor input."""
        return bridge.click(x, y)

    @server.tool(name="press_game_key")
    @_tool_errors
    def key_in_game(key: str, hold: float = 0.08) -> dict:
        """Send a supported key inside GD only; never to the active desktop application."""
        return bridge.press(key, hold)

    @server.tool(name="playtest_inputs", structured_output=False)
    @_tool_errors
    def play_game(events: list[dict], duration: float):
        """Run ordered {at,key,action:down|up} events inside GD, returning game evidence and an image.
    
        Open the level first. Timing is OS wall-clock, not physics-step precise.
        Real PlayLayer state establishes deaths/completion; none are assumed.
        """
        result = bridge.run_inputs(events, duration)
        return [json.dumps(result), Image(path=Path(result["image"]))]

    return server


def serve() -> None:
    make_server().run(transport="stdio")
