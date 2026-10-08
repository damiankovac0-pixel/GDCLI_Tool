# GDCLI_Tool

A free, local Geometry Dash authoring tool an AI coding chat or MCP client can drive. Tell your AI the theme, gameplay, duration and music; it builds a structured level, creates it in your signed-in game account, opens the real editor, inspects rendered frames, plays it, and revises it from your feedback.

**Implemented, not a planning scaffold.** Python handles authoring/export and encrypted backups. A small Geode mod handles actual game actions through a private Unix socket. No desktop mouse/keyboard automation, model API key, hosted server, copied community levels, or bundled music.

The bundled demo is only a **capability check**, not a finished level or a quality benchmark. Creating and controlling a level is proven; good gameplay, decoration and music synchronization still require actual design work and feedback.

## Supported setup

| Component | Exercised configuration |
| --- | --- |
| Computer | Apple Silicon macOS; Darwin 25.5 |
| Game | Steam Geometry Dash **2.2081**; its bundle reports `2.208` |
| Loader | Geode **5.10.1** |
| Python | **3.12.8**, managed by uv |
| Interface | JSON CLI and MCP **2.3** over stdio |
| Gameplay proof | Original 215-object classic cube course; normal-mode completion, zero deaths |
| Background proof | Editor opening, revision, capture, save/exit and full gameplay while Zed remained active and the cursor stayed unchanged |

The released native package targets **macOS arm64**. Windows, Linux, Intel Macs, platformer gameplay and other game patches are not claimed as exercised configurations. Compilation and `.gmd` export are portable; live account/game integration is currently macOS-only.

**Background use:** once the game is running, you can work in other applications. The bridge keeps the game updating when inactive, but does not undo a deliberate in-game pause. It still uses normal game CPU/GPU and audio resources. Steam/macOS can bring the game forward during startup; switch back to your other app afterward. The ongoing tool actions do not activate it.

## Install

Requirements: a legitimate Steam game installation, [uv](https://docs.astral.sh/uv/), and your existing AI CLI/chat if you want natural-language planning. The tool itself makes no model-provider calls; your AI client's own model/subscription arrangements are separate.

```sh
git clone https://github.com/damiankovac0-pixel/GDCLI_Tool.git
cd GDCLI_Tool
uv sync --locked
```

Download `pandagamer.gdcli.geode` from the repository's [GitHub release](https://github.com/damiankovac0-pixel/GDCLI_Tool/releases). Save/close Geometry Dash normally, then:

```sh
uv run gdcli install --bridge /absolute/path/pandagamer.gdcli.geode
uv run gdcli game open
uv run gdcli doctor
```

`install` downloads the pinned official Geode macOS installer and checks its GitHub release SHA-256. It **expands** the package without executing its privileged interactive script. It backs up the original executable, FMOD library and encrypted saves, then installs only game-local loader files and our mod. No sudo is required. An existing Geode installation is left in place; only our bridge is added. `--package /path/geode-installer-v5.10.1-mac.pkg` uses a local copy of that same checked official installer.

Sign in **inside the game** if necessary. Never give the AI your password, GJP/GJP2 value or Steam credentials. `doctor` exposes only allowlisted account identity and integration status. Native scene captures need neither macOS Accessibility nor Screen Recording permission.

## Use from this chat or your CLI

An AI with shell access to this checkout can run these commands directly. No MCP registration is needed for that workflow.

```sh
# Original example; creates a readable JSON authoring document.
uv run gdcli demo --output outputs/neon-run.json
uv run gdcli create outputs/neon-run.json --open editor
uv run gdcli game capture

# Save/exit the editor, then play using the actual engine.
uv run gdcli game leave
uv run gdcli game level "Chat Neon Run" --mode play
uv run gdcli game play --seconds 18

# After feedback, revise the JSON, leave the current scene, and update.
uv run gdcli game leave
uv run gdcli update outputs/neon-run.json --open editor

# Portable file export and object lookup.
uv run gdcli compile outputs/neon-run.json --output outputs/neon-run.gmd
uv run gdcli objects "yellow orb"
```

`create` adds a **new** level owned by the signed-in account and refuses duplicate names. `update` only revises names this tool previously created in that same account. Existing personal levels cannot be updated through that command. Both checkpoint the game and retain an exact encrypted local-level backup before changing anything. Neither uploads levels.

`game leave` uses normal editor **save and exit**, or normal play exit; it never discards editor edits. Create/update/open refuse unsafe transitions out of an open editor. Leave play before updating its geometry or opening another level.

Commands return JSON; failures return JSON on stderr with a nonzero exit code. `game key space --hold 0.15` and `game click X Y` act **inside the game only**. Clicks activate real enabled Cocos menu items at normalized top-left coordinates; they do not move your cursor. Timed play inputs accept a JSON array:

```json
[
  {"at": 0.3, "key": "space", "action": "down"},
  {"at": 0.4, "key": "space", "action": "up"}
]
```

```sh
uv run gdcli game play --seconds 10 --inputs /path/inputs.json
```

Input timing is **OS wall-clock, not physics-step deterministic**. Inputs are validated before sending any; held keys are released on failures. A newly opened play scene has the game's normal entry delay: inspect `player_x`/`player_y` and wait for actual play before timing the first jump. Actual PlayLayer state reports progress, deaths and completion. A screenshot or successful compilation alone is not a completion claim.

### Authoring document

```json
{
  "name": "My Original Level",
  "description": "Made from a brief",
  "song_id": 0,
  "settings": {"kA6": 1},
  "objects": [
    {"id": 1, "x": 15, "y": -15, "properties": {"21": 1}},
    {"id": 35, "x": 450, "y": 10},
    {"id": 8, "x": 540, "y": 15, "properties": {"21": 2}}
  ]
}
```

- `song_id` references built-in game music. Alternatively use `custom_song_id`; both cannot be nonzero. Custom-song availability/download remains the game's responsibility.
- `settings` uses real GD start keys; object `properties` uses numeric GD property-key strings. Unknown properties are retained rather than silently stripped.
- Coordinates are **serialized editor coordinates**: ground boundary `y=0`, grounded spike center `y=15`, floor block center `y=-15`. The engine adds 90 to world Y; runtime grounded cube center is about `105`. Do not place hazards at serialized `105` expecting them to touch the grounded player.
- The compiler checks finite coordinates, primitive types, conflicting typed/raw properties, delimiter injection and metadata. It is a **format validator**, not a physics simulator or complete object-property schema.
- Object order and compiled exports are deterministic. The bundled object catalog is a reference; use real-game inspection for unfamiliar objects, modes and triggers.
- Feedback iteration is ordinary JSON editing followed by backed-up `update`. Re-run gameplay and visual inspection after every material change. A completion does not establish human difficulty, music synchronization or rated-level artistic quality.

## MCP setup

For an MCP-capable AI client, use this stdio server configuration after `uv sync`:

```json
{
  "mcpServers": {
    "gdcli": {
      "command": "/absolute/path/GDCLI_Tool/.venv/bin/python",
      "args": ["-m", "gdaitrans", "serve"]
    }
  }
}
```

Tools include `doctor`, `account_status`, `find_objects`, `list_levels`, `validate_level`, `export_level`, `create_level`, `update_level`, `open_game`, `open_level`, `leave_level`, `game_state`, `capture_game`, `press_game_key`, `click_game`, `playtest_inputs` and `close_game`. Capture/play tools return real PNG image content for vision-capable clients. The AI remains the planner; this package does not pretend to contain an autonomous language model.

Recommended loop: inspect the account/integration, design a complete original JSON document, create it, open/capture the editor, save/exit, run normal gameplay, inspect deaths/progress/images, revise only the authored level, then repeat. User feedback supplies difficulty and artistic judgments that one automated completion cannot establish.

## Private data, recovery and removal

- Socket: `~/Library/Application Support/GDAITRANS/bridge.sock`; private directory `0700`, socket `0600`, same-user peers only. No TCP listener or arbitrary-code RPC.
- Backups, captures, installation manifest and authored-level ownership records remain under that private directory, never in GitHub.
- Python never writes live game saves. The bridge saves through the actual game. `import-save` is a separate, explicit stopped-game path with atomic replacement, an exact encrypted backup, stale-file checks and append-only behavior.
- No force-kill, automatic level upload, noclip, fabricated verification/completion, account login automation, or game-asset redistribution. Revisions call the game's normal `levelWasAltered` invalidation rather than carrying a fake verified state onto changed geometry.
- A bridge disconnect or timeout is an error, not success. Inspect state/listed levels before retrying a mutation; a disconnect after the game applied it can leave a completed operation without its response.

To remove the managed integration, save/leave your level and close the game normally:

```sh
uv run gdcli game leave
uv run gdcli game close
uv run gdcli uninstall
```

This restores the backed-up original library when our installer added the loader. It removes only our managed files, leaves existing Geode installations/other mods alone, and **does not restore or overwrite your current levels**. Backups remain. Files changed outside the tool are not silently removed. Steam updates can replace loader files; re-check compatibility before reinstalling.

## Build the bridge from source

Pinned dependencies:

- Geode SDK `v5.10.1`, commit `7e41336f68660b990644f5c3450b6da6ae944560`.
- Bindings `7f6c2a75742856de88dad354e576dcff8a28e881`, matching that loader release.
- Geode CLI `v3.9.0`; CMake 3.25+; a compatible C++23 Apple toolchain.

Install the SDK/CLI using [Geode's developer setup](https://docs.geode-sdk.org/getting-started/geode-cli). Configure the actual macOS game profile, set the SDK path, and install the SDK's `mac` binaries. Then:

```sh
cmake -S native/bridge -B outputs/native-build \
  -DGEODE_SDK=/absolute/path/geode-sdk \
  -DGEODE_BINDINGS_REPO_PATH=/absolute/path/bindings \
  -DCMAKE_OSX_ARCHITECTURES=arm64 \
  -DCMAKE_BUILD_TYPE=Release \
  -DGEODE_CLI=/absolute/path/geode
cmake --build outputs/native-build --parallel 4
uv run gdcli install --bridge outputs/native-build/pandagamer.gdcli.geode
```

On macOS, game work must be scheduled with **Geode's engine-thread queue**, not AppKit's GCD main queue. The engine owns its Cocos autorelease pool and OpenGL context on a separate thread. That distinction applies to saving, scene creation and capture—not just input.

Run safety regressions with `uv run python -m unittest discover -s tests -v`. Runtime verification must also exercise the actual game while a different app is active; mocked tests are not proof of background control or playability.

## Existing projects and the gap

This is not the first AI/programmable GD project. The useful gap here is a **local, chat-callable create → real-game inspect/play → feedback/revise loop that does not take over the desktop**. It is a personal tool, not a claim of market uniqueness or guaranteed commercial demand.

| Project | Role here |
| --- | --- |
| [Geode](https://github.com/geode-sdk/geode) — Boost Software License | Actual in-game integration/runtime; pinned SDK/loader dependency. |
| [GMDKit](https://github.com/UHDanke/gmdkit) — MIT | Bundled object-catalog data, pinned to `1f5315cbc49efff5d05b6d2e17380ad88f95b241`; upstream license retained beside the CSV. Its whole-save rewrite is **not** used. |
| [G.js](https://github.com/g-js-api/G.js) — ISC | Native macOS save/format reference, pinned to `249b13374114fb5c27018e4ae887320474f4d896`; not a runtime dependency. Ambiguous historical double-padding is not guessed. |
| [GD Docs](https://github.com/Wyliemaster/gddocs) | Community format reference at `a50d30beade9991963063d11687d0beff9abbcc1`; older property tables are incomplete for 2.2. |
| [EditorAI](https://github.com/entity12208/EditorAI) — MIT | Existing AI-authoring competitor/reference. Its simulation is not substituted for actual game play. |
| [WSLiveEditor](https://github.com/iAndyHD3/WSLiveEditor) | API reference for live editor actions; no source copied into this repository. |
| [GD RL](https://github.com/dylanelu/geometry-dash-ai) | Reference for Geode-based control. No RL training stack or copied level collection is included. |
| [SPWN](https://github.com/Spu7Nix/SPWN), [GDShare](https://github.com/HJfod/GDShare) | Related procedural authoring and `.gmd` interchange tools; not required for this local bridge. |

We reuse the appropriate data/runtime and reference the other work, rather than blindly merging incompatible applications. Direct GitHub/manual distribution is separate from Geode's public mod index: its [generative-AI index rules](https://docs.geode-sdk.org/mods/guidelines/#mods-using-generative-ai) restrict AI object placement/modification submissions there. That is not presented as a blanket RobTop ban. This tool does not automatically submit a mod to that index or publish a GD level online.

Project code is MIT-licensed; bundled third-party data retains its own license. Geometry Dash and its assets are not included.
