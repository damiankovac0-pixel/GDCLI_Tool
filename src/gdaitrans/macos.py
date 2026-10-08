"""Discover, launch, and capture GD without taking over the user's desktop.

Game controls live in the in-process Unix-socket bridge, not OS input events.
"""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

BUNDLE_ID = "com.robtop.geometrydashmac"


def _native():
    if sys.platform != "darwin":
        raise RuntimeError("native game integration currently requires macOS")
    import AppKit
    import Quartz
    return AppKit, Quartz


def _application():
    AppKit, _ = _native()
    return next((app for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                 if app.bundleIdentifier() == BUNDLE_ID), None)


def app_path() -> Path:
    configured = os.environ.get("GDAITRANS_GAME_APP")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_dir():
            raise RuntimeError(f"GDAITRANS_GAME_APP is not an application directory: {path}")
        return path
    path = Path.home() / "Library/Application Support/Steam/steamapps/common/Geometry Dash/Geometry Dash.app"
    if path.is_dir():
        return path
    AppKit, _ = _native()
    url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(BUNDLE_ID)
    if url is not None:
        return Path(url.path())
    raise RuntimeError("Geometry Dash was not found; set GDAITRANS_GAME_APP to its .app path")


def status() -> dict:
    _, Quartz = _native()
    app = _application()
    result = {"platform": sys.platform, "running": app is not None,
              "screen_recording": bool(Quartz.CGPreflightScreenCaptureAccess()), "window": None}
    try:
        installed = app_path()
        with (installed / "Contents/Info.plist").open("rb") as file:
            info = plistlib.load(file)
        result.update(app=str(installed), version=info.get("CFBundleShortVersionString"))
    except (OSError, RuntimeError, plistlib.InvalidFileException) as error:
        result["installation_error"] = str(error)
    if app is None:
        return result
    pid = int(app.processIdentifier())
    result.update(pid=pid, active=bool(app.isActive()))
    windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)
    candidates = [window for window in windows if window.get("kCGWindowOwnerPID") == pid
                  and window.get("kCGWindowLayer") == 0
                  and window["kCGWindowBounds"]["Width"] >= 200
                  and window["kCGWindowBounds"]["Height"] >= 100]
    if candidates:
        window = max(candidates, key=lambda item: (bool(item.get("kCGWindowIsOnscreen")),
                         item["kCGWindowBounds"]["Width"] * item["kCGWindowBounds"]["Height"]))
        result["window"] = {"id": int(window["kCGWindowNumber"]),
                            "bounds": dict(window["kCGWindowBounds"]),
                            "visible": bool(window.get("kCGWindowIsOnscreen"))}
    return result


def _run_loop_step() -> None:
    # Cocoa's run loop refreshes NSWorkspace process state after launch/quit.
    from Foundation import NSDate, NSRunLoop
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))


def open_game(timeout: float = 25.0) -> dict:
    """Launch in the background. Never activate GD or move the user's cursor."""
    _native()
    if _application() is None:
        path = app_path()
        # Steam's DRM startup exits when the app bundle is opened directly.
        target = "steam://rungameid/322170" if "steamapps" in path.parts else str(path)
        subprocess.run(["open", "-g", target], check=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = status()
        if current["running"] and current["window"]:
            return current
        _run_loop_step()
    raise RuntimeError("Geometry Dash did not expose a game window; check Steam and its launch screen")


def close_game(timeout: float = 20.0) -> dict:
    """Request a normal quit for installation, never kill or dismiss edit dialogs."""
    app = _application()
    if app is None:
        return {"running": False, "closed": True}
    if not app.terminate():
        raise RuntimeError("Geometry Dash declined to quit; save/close it in the game first")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _application() is None:
            return {"running": False, "closed": True}
        _run_loop_step()
    raise RuntimeError("Geometry Dash is still running; resolve its save/quit dialog")


def capture_window(destination: str | Path = "outputs/window.png") -> dict:
    """Independent native window capture; no focus change or desktop input."""
    current = status()
    if not current["running"] or not current["window"]:
        raise RuntimeError("Geometry Dash needs a window before it can be captured")
    if not current["screen_recording"]:
        raise RuntimeError("Window capture requires Screen Recording permission; the in-game bridge has its own capture")
    path = Path(destination).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    _, Quartz = _native()
    from Foundation import NSURL
    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow,
        current["window"]["id"], Quartz.kCGWindowImageBoundsIgnoreFraming,
    )
    if image is None:
        raise RuntimeError("macOS could not capture the game-window backing image")
    output = Quartz.CGImageDestinationCreateWithURL(NSURL.fileURLWithPath_(str(path)), "public.png", 1, None)
    if output is None:
        raise RuntimeError(f"macOS could not create the PNG destination: {path}")
    Quartz.CGImageDestinationAddImage(output, image, None)
    if not Quartz.CGImageDestinationFinalize(output):
        raise RuntimeError("macOS failed to write the game-window PNG")
    return {"image": str(path), "window": current["window"]}
