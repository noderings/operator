"""Java Minecraft refuses to boot unless eula.txt contains eula=true.

That file is the only acceptance mechanism, from Java Edition 1.7.10 onward.
No later version, including the 26.x line, accepts the agreement through
another file, variable, or flag. Bedrock, proxies, and Java builds older
than 1.7.10 do not read eula.txt.
"""

from __future__ import annotations

from typing import Any

_EXCLUDED_WORDS = (
    "bedrock",
    "proxy",
    "proxies",
    "bungee",
    "velocity",
    "waterfall",
    "waterdog",
    "pocketmine",
    "nukkit",
    "endstone",
    "terraria",
)

_EXCLUDED_KEYS = {
    "BUNGEE_VERSION",
    "VELOCITY_VERSION",
    "WATERDOG_VERSION",
    "BEDROCK_VERSION",
    "NUKKIT_VERSION",
    "ENDSTONE_VERSION",
    "TERRARIA_VERSION",
}

_JAVA_VERSION_KEYS = {
    "MC_VERSION",
    "MINECRAFT_VERSION",
    "FORGE_VERSION",
    "NEOFORGE_VERSION",
    "FABRIC_VERSION",
}

_JAVA_NAME_MARKS = (
    "forge",
    "fabric",
    "neoforge",
    "purpur",
    "paper",
    "spigot",
    "bukkit",
    "quilt",
    "sponge",
    "folia",
    "modpack",
)


def java_minecraft_server(egg_name: str, environment: dict[str, Any] | None) -> bool:
    """True when this server will exit until eula.txt says eula=true."""
    name = (egg_name or "").casefold()
    if any(word in name for word in _EXCLUDED_WORDS):
        return False
    keys = {str(key).upper() for key in (environment or {})}
    if keys & _EXCLUDED_KEYS:
        return False
    if keys & _JAVA_VERSION_KEYS:
        return True
    return any(mark in name for mark in _JAVA_NAME_MARKS)
