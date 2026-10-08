# GDCLI_Tool operating contract

This repository's `gdcli` CLI and MCP stdio server are the AI's tools; the AI supplies the design. No model API key belongs in this repository. Read README.md for setup, supported versions and the JSON authoring contract.

## Before authoring

- Run `uv run gdcli doctor`. Require the user's actual signed-in account for live creation; ask them to log in inside the game if needed. Never ask for or print passwords, tokens, GJP/GJP2 or Steam credentials.
- Keep specs/exports under ignored `outputs/`; keep private saves, screenshots, backups and ownership records out of commits/releases.
- Use original gameplay and decoration. Search actual IDs using `gdcli objects`; don't copy community levels or redistribute music/game assets.
- Respect a user-requested stop: no new level edits, scene changes, captures or playtests until they ask to resume. Successful insertion proves integration, not design quality.

## Learn the user's design taste

The bundled demo is an integration probe, not a style template. Never expand it and call that a polished level.

1. Ask for a reference level ID, video or screenshots, plus the desired difficulty and music. Inspect the supplied reference before claiming to understand it; do not invent familiarity with unseen levels.
2. Extract a concrete brief: palette, contrast, block design, foreground/background separation, repeated motifs, transition language, gameplay rhythm and musical phrases. Reference principles, not copied object arrangements.
3. With the user's permission to resume authoring, develop one short musical section first. Establish readable gameplay before decoration; measure real movement/timing rather than guessing beat positions from X alone.
4. Use a consistent visual hierarchy: clearly distinguish solid surfaces and hazards from decoration, limit competing accents, reuse intentional motifs, and keep effects subordinate to gameplay readability. Review actual in-game frames, not just object counts.
5. Ask the user to critique that section before expanding the full level. Record approved choices and explicit dislikes in the private authoring document's description or an adjacent ignored brief; carry them into subsequent sections and sessions.
6. Review variation, transitions, pacing and music synchronization separately from technical correctness. Completion does not establish fun, calibrated difficulty or good art. Do not claim quality before the user approves it.

This is reference-guided iteration with persistent design notes, not a claim that chatting retrains model weights. Do not add a training stack or generate another full demo to substitute for learning the user's taste.

## Create and iterate

1. Build a complete structured JSON authoring document. Validate/export it with the deterministic compiler; compilation is not proof of playability.
2. Create a new level with `gdcli create`. No duplicate overwrites. Only `gdcli update` may revise an existing tool-authored name, and only in its original account.
3. Open/capture the actual editor. Inspect the rendered result and actual object count.
4. `gdcli game leave` saves/exits the editor or quits play normally. Never discard unrelated edits, overwrite existing personal levels, force-kill the game, or mutate live save files from Python.
5. Open normal play. Execute timed inputs through the in-game bridge and inspect actual progress/deaths/completion plus real PNG frames. Do not infer success from elapsed time, a mock, or a simulator.
6. Use feedback to revise the spec, retain the automatic encrypted backup, and re-run the whole level. Do not claim rated-level quality, calibrated difficulty, beat sync, platformer support, or untested game versions from one successful cube run.

## Keep the desktop usable

- No HID/CGEvent keyboard or mouse posting, OS-level clicking, cursor movement, clipboard takeover, or app activation in control paths.
- The game may focus during Steam startup. Once running, authoring/capture/input must work while another app stays active. Verify actual foreground app/cursor state with refreshed Cocoa observation when changing this behavior.
- Native game operations belong on **Geode's Cocos engine queue**. macOS/AppKit's GCD main queue is a different thread and lacks the correct Cocos autorelease/OpenGL context.
- Preserve deliberate in-game pause. Background play still consumes normal CPU/GPU/audio resources; don't hide that fact.

## Changes and verification

Use the existing CLI/compiler/backup/bridge conventions. Preserve unknown GD properties. Never fake verification/completion; use normal game invalidation for changed geometry. Keep the Unix endpoint private and same-user-only; no arbitrary-code or unauthenticated network service.

Run `uv run python -m unittest discover -s tests -v` for safety regressions, then exercise the changed behavior in the real game. Permanent tests should cover user-visible boundaries, errors, preservation and input cleanup—not mocked forwarding echoes or implementation text. Keep throwaway runtime scripts out of commits.

The tool is distributed directly through GitHub. Geode's public index has separate generative-AI submission restrictions; don't conflate those with RobTop's policies or auto-submit this mod/levels.
