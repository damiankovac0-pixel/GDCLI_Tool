"""An original short neon cube course; no copied level or bundled music data."""

from __future__ import annotations

import base64


def neon_run(name: str = "Chat Neon Run") -> dict:
    """Return a complete authoring document with automatic pads and clear hazards.

    Uses the game's built-in Stereo Madness track (ID 0), not redistributed audio.
    Actual completion must be established in the game, not assumed by this builder.
    """
    colors = {
        1000: (8, 12, 28), 1001: (13, 22, 40), 1002: (101, 218, 255),
        1: (88, 223, 255), 2: (255, 151, 105), 3: (201, 112, 255),
        4: (27, 47, 70), 5: (226, 242, 255),
    }
    color_string = "".join(
        f"1_{r}_2_{g}_3_{b}_4_-1_6_{channel}_7_1_15_1_18_0|"
        for channel, (r, g, b) in colors.items()
    )
    objects: list[dict] = []

    def add(object_id: int, x: float, y: float, **properties) -> None:
        # Serialized GD Y=0 is the ground boundary; runtime geometry adds 90.
        objects.append({"id": object_id, "x": x, "y": y - 90,
                        "properties": {str(key): value for key, value in properties.items()}})

    def text(value: str, x: float, y: float, scale: float = 0.6) -> None:
        add(914, x, y, **{"31": base64.b64encode(value.encode()).decode(),
                         "32": scale, "21": 5, "24": 7, "20": 2})

    # A consistent architectural rail, below the stock game's ground line (90).
    for x in range(15, 4110, 30):
        add(1, x, 75, **{"21": 1, "22": 4, "20": 1})
        if x % 120 == 15:
            add(503, x, 100, **{"21": 1, "32": 1.0, "24": -1, "121": True, "20": 2})

    text("CHAT NEON RUN", 260, 315, 0.85)
    text("A LEVEL MADE FROM CHAT", 260, 270, 0.4)
    text("FOLLOW THE GOLD PADS", 260, 220, 0.45)

    # Yellow pads at the floor launch the cube over two readable spikes.
    # Their placement is intentional gameplay, not altered physics or fake flags.
    for section, pad_x in enumerate((450, 1050, 1650, 2250, 2850, 3450)):
        add(35, pad_x, 100, **{"20": 0})
        for spike_x in (pad_x + 90, pad_x + 120):
            add(8, spike_x, 105, **{"21": 2, "20": 0})
        # Glowing pillars frame each motif, well above the player path.
        for offset in (-110, 270):
            add(503, pad_x + offset, 250, **{"21": 3 if section % 2 else 1,
                "6": 90, "32": 5, "24": -3, "121": True, "20": 2})
        text(f"0{section + 1}", pad_x + 150, 300, 0.5)

    # Slow background changes, not strobing effects.
    for x, rgb in ((950, (22, 10, 39)), (2050, (7, 30, 38)), (3150, (8, 12, 28))):
        add(899, x, 420, **{"7": rgb[0], "8": rgb[1], "9": rgb[2],
                            "10": 1.4, "23": 1000, "20": 3})
    text("FINISH", 3990, 280, 0.8)
    return {
        "name": name,
        "description": "Original neon cube course created from chat. Follow the yellow pads through six motifs.",
        "song_id": 0,
        "settings": {"kA6": 1, "kA7": 1, "kS38": color_string},
        "objects": objects,
    }
