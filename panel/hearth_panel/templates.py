"""Game templates (Pterodactyl calls these "eggs").

A template says which image to run, which ports it needs, where data lives, and which
environment-backed variables the user may configure. Add a game by adding a Template here.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal

VarType = Literal["string", "int", "bool", "select", "text"]
VarGroup = Literal["startup", "properties"]


@dataclass(frozen=True)
class Variable:
    key: str  # environment variable name
    label: str
    type: VarType = "string"
    default: Any = ""
    group: VarGroup = "properties"
    description: str = ""
    options: tuple[str, ...] = ()
    min: int | None = None
    max: int | None = None
    pattern: str | None = None
    secret: bool = False
    required: bool = False
    admin_only: bool = False  # can run arbitrary code on the node; owners can't change it


@dataclass(frozen=True)
class TemplatePort:
    name: str
    protocols: tuple[str, ...]
    container_port: int | None = None  # None: same number as the host port
    offset: int = 0  # host port = primary port + offset


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    game: str
    description: str
    image: str
    data_mount: str
    ports: tuple[TemplatePort, ...]
    variables: tuple[Variable, ...]
    default_memory_mb: int
    min_memory_mb: int
    preferred_port: int = 0  # allocation search starts here (the game's conventional port)
    console: Literal["stdin", "exec"] = "stdin"
    console_exec: tuple[str, ...] = ()
    stop_timeout: int = 60
    features: tuple[str, ...] = ()  # "minecraft_java", "modpacks", "custom_image", "eula"
    fixed_env: dict[str, str] = field(default_factory=dict)
    env_from_port: dict[str, str] = field(default_factory=dict)  # ENV -> port name
    image_for: Callable[[dict[str, Any]], str] | None = None
    pre_backup: tuple[str, ...] = ()  # console commands that make files consistent for a backup
    post_backup: tuple[str, ...] = ()
    world_folder_var: str | None = None  # variable naming the world folder (for import/export)

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("image_for", None)
        data.pop("fixed_env", None)
        data.pop("env_from_port", None)
        return data


# ----- Minecraft Java helpers ---------------------------------------------------------------

MC_SERVER_TYPES = ("VANILLA", "PAPER", "PURPUR", "FABRIC", "QUILT", "FORGE", "NEOFORGE")

_RELEASE_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


def parse_mc_version(version: str) -> tuple[int, int, int] | None:
    """Parse '1.20.4', '1.21', or the year-based '26.1' / '26.1.2' scheme."""
    m = _RELEASE_RE.match(version or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def java_tag_for(version: str | None) -> str:
    """Pick the itzg/minecraft-server image tag whose Java runtime fits a Minecraft version."""
    parsed = parse_mc_version(version or "")
    if parsed is None:  # "LATEST", snapshots like 24w14a, or unknown: newest runtime
        return "latest"
    major, minor, patch = parsed
    if major >= 2:  # year-based versions (26.x+) need the newest Java
        return "latest"
    if minor <= 16:
        return "java8"
    if (minor, patch) < (20, 5):
        return "java17"
    return "java21"


def _minecraft_image(values: dict[str, Any]) -> str:
    version = values.get("_MC_VERSION") or values.get("VERSION")
    return f"itzg/minecraft-server:{java_tag_for(str(version) if version else None)}"


MC_JAVA_VARIABLES: tuple[Variable, ...] = (
    Variable("TYPE", "Server software", "select", "PAPER", "startup",
             "Vanilla, a plugin server, or a mod loader. Modpacks set this automatically.",
             options=MC_SERVER_TYPES),
    Variable("VERSION", "Minecraft version", "string", "LATEST", "startup",
             "Any release or snapshot id, or LATEST.", pattern=r"^[A-Za-z0-9._\-]{1,32}$"),
    Variable("USE_AIKAR_FLAGS", "Aikar's GC flags", "bool", True, "startup",
             "Tuned garbage-collector flags; recommended for most servers."),
    Variable("TZ", "Time zone", "string", "UTC", "startup", "IANA zone, e.g. America/Toronto.",
             pattern=r"^[A-Za-z0-9_+\-/]{1,64}$"),
    Variable("MOTD", "Message of the day", "string", "A Hearth server", "properties", max=120),
    Variable("DIFFICULTY", "Difficulty", "select", "normal", "properties",
             options=("peaceful", "easy", "normal", "hard")),
    Variable("MODE", "Game mode", "select", "survival", "properties",
             options=("survival", "creative", "adventure", "spectator")),
    Variable("MAX_PLAYERS", "Max players", "int", 20, "properties", min=1, max=1000),
    Variable("VIEW_DISTANCE", "View distance", "int", 10, "properties", "In chunks.", min=2, max=32),
    Variable("SIMULATION_DISTANCE", "Simulation distance", "int", 10, "properties", min=2, max=32),
    Variable("SPAWN_PROTECTION", "Spawn protection", "int", 16, "properties", "Radius in blocks; 0 disables.",
             min=0, max=256),
    Variable("PVP", "PvP", "bool", True, "properties"),
    Variable("HARDCORE", "Hardcore", "bool", False, "properties"),
    Variable("ALLOW_FLIGHT", "Allow flight", "bool", False, "properties",
             "Turn on for modpacks with jetpacks or flying mounts to avoid kicks."),
    Variable("ALLOW_NETHER", "Allow Nether", "bool", True, "properties"),
    Variable("ENABLE_COMMAND_BLOCK", "Command blocks", "bool", False, "properties"),
    Variable("SPAWN_MONSTERS", "Spawn monsters", "bool", True, "properties"),
    Variable("ONLINE_MODE", "Online mode", "bool", True, "properties",
             "Verify accounts with Mojang. Only disable behind a proxy like Velocity."),
    Variable("ENABLE_WHITELIST", "Whitelist", "bool", False, "properties"),
    Variable("WHITELIST", "Whitelisted players", "string", "", "properties",
             "Comma-separated usernames.", pattern=r"^[A-Za-z0-9_,\s]*$"),
    Variable("OPS", "Operators", "string", "", "properties", "Comma-separated usernames.",
             pattern=r"^[A-Za-z0-9_,\s]*$"),
    Variable("LEVEL", "World name", "string", "world", "properties", pattern=r"^[A-Za-z0-9_\-]{1,64}$"),
    Variable("SEED", "World seed", "string", "", "properties", max=64),
    Variable("LEVEL_TYPE", "World type", "select", "minecraft:normal", "properties",
             options=("minecraft:normal", "minecraft:flat", "minecraft:large_biomes", "minecraft:amplified")),
)


TEMPLATES: dict[str, Template] = {
    t.key: t
    for t in (
        Template(
            key="minecraft-java",
            name="Minecraft: Java Edition",
            game="Minecraft",
            description="Vanilla, Paper, Fabric, Forge, NeoForge and modpacks from CurseForge or Modrinth.",
            image="itzg/minecraft-server:latest",
            image_for=_minecraft_image,
            data_mount="/data",
            ports=(TemplatePort("game", ("tcp",), container_port=25565),),
            variables=MC_JAVA_VARIABLES,
            default_memory_mb=4096,
            min_memory_mb=1024,
            preferred_port=25565,
            console="exec",
            console_exec=("rcon-cli",),
            stop_timeout=90,
            features=("minecraft_java", "modpacks", "eula", "players", "mods"),
            fixed_env={"EULA": "TRUE", "ENABLE_RCON": "TRUE"},
            pre_backup=("save-off", "save-all flush"),
            post_backup=("save-on",),
            world_folder_var="LEVEL",
        ),
        Template(
            key="minecraft-bedrock",
            name="Minecraft: Bedrock Edition",
            game="Minecraft",
            description="Official Bedrock Dedicated Server for console, mobile and Windows players.",
            image="itzg/minecraft-bedrock-server:latest",
            data_mount="/data",
            ports=(TemplatePort("game", ("udp",)),),
            env_from_port={"SERVER_PORT": "game"},
            variables=(
                Variable("VERSION", "Bedrock version", "string", "LATEST", "startup",
                         "LATEST, PREVIEW, or a specific version like 1.21.50.07.",
                         pattern=r"^[A-Za-z0-9.]{1,32}$"),
                Variable("SERVER_NAME", "Server name", "string", "Hearth Bedrock", "properties", max=64),
                Variable("GAMEMODE", "Game mode", "select", "survival", "properties",
                         options=("survival", "creative", "adventure")),
                Variable("DIFFICULTY", "Difficulty", "select", "normal", "properties",
                         options=("peaceful", "easy", "normal", "hard")),
                Variable("MAX_PLAYERS", "Max players", "int", 10, "properties", min=1, max=200),
                Variable("ALLOW_CHEATS", "Allow cheats", "bool", False, "properties"),
                Variable("ONLINE_MODE", "Online mode", "bool", True, "properties"),
                Variable("VIEW_DISTANCE", "View distance", "int", 32, "properties", min=5, max=96),
                Variable("LEVEL_NAME", "World name", "string", "Bedrock level", "properties", max=64),
                Variable("LEVEL_SEED", "World seed", "string", "", "properties", max=64),
            ),
            default_memory_mb=2048,
            min_memory_mb=512,
            preferred_port=19132,
            console="exec",
            console_exec=("send-command",),
            features=("eula",),
            fixed_env={"EULA": "TRUE"},
            pre_backup=("save hold",),
            post_backup=("save resume",),
        ),
        Template(
            key="valheim",
            name="Valheim",
            game="Valheim",
            description="Dedicated Valheim server. Uses two consecutive UDP ports.",
            image="lloesche/valheim-server:latest",
            data_mount="/config",
            ports=(TemplatePort("game", ("udp",)), TemplatePort("query", ("udp",), offset=1)),
            env_from_port={"SERVER_PORT": "game"},
            variables=(
                Variable("SERVER_NAME", "Server name", "string", "Hearth Valheim", "properties", max=64),
                Variable("WORLD_NAME", "World name", "string", "Dedicated", "properties",
                         pattern=r"^[A-Za-z0-9_\-]{1,64}$"),
                Variable("SERVER_PASS", "Password", "string", "", "properties",
                         "At least 5 characters, and not part of the server name.",
                         pattern=r"^.{5,64}$", secret=True, required=True),
                Variable("SERVER_PUBLIC", "List publicly", "bool", False, "properties"),
                Variable("TZ", "Time zone", "string", "UTC", "startup", pattern=r"^[A-Za-z0-9_+\-/]{1,64}$"),
            ),
            default_memory_mb=4096,
            min_memory_mb=2048,
            preferred_port=2456,
            stop_timeout=120,
        ),
        Template(
            key="custom",
            name="Custom Docker image",
            game="Other",
            description="Run any game server image. You choose the image, port and data path.",
            image="",
            data_mount="/data",
            ports=(TemplatePort("game", ("tcp", "udp")),),
            variables=(
                Variable("IMAGE", "Docker image", "string", "", "startup", "e.g. ghcr.io/owner/game:tag",
                         pattern=r"^[a-z0-9][a-z0-9._\-/:@]{0,254}$", required=True, admin_only=True),
                Variable("CONTAINER_PORT", "Container port", "int", 0, "startup",
                         "The port the game listens on inside the container; 0 uses the host port.",
                         min=0, max=65535, admin_only=True),
                Variable("DATA_PATH", "Data path", "string", "/data", "startup",
                         "Container directory that holds saves and config.", pattern=r"^/[A-Za-z0-9._\-/]*$",
                         admin_only=True),
                Variable("EXTRA_ENV", "Environment", "text", "", "startup",
                         "One KEY=value per line, passed to the container.", admin_only=True),
            ),
            default_memory_mb=2048,
            min_memory_mb=128,
            features=("custom_image",),
        ),
    )
}


class TemplateError(ValueError):
    pass


def get_template(key: str) -> Template:
    try:
        return TEMPLATES[key]
    except KeyError as exc:
        raise TemplateError(f"Unknown template {key!r}") from exc


def coerce_variables(template: Template, values: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate user-supplied variable values against the template; unknown keys are rejected."""
    known = {v.key: v for v in template.variables}
    result = dict(base or {})
    for key, raw in values.items():
        var = known.get(key)
        if var is None:
            raise TemplateError(f"Unknown setting {key!r}")
        result[key] = _coerce(var, raw)
    return result


def _coerce(var: Variable, raw: Any) -> Any:
    if var.type == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in ("true", "false", "1", "0"):
            return raw.lower() in ("true", "1")
        raise TemplateError(f"{var.label} must be on or off")
    if var.type == "int":
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise TemplateError(f"{var.label} must be a whole number") from exc
        if var.min is not None and value < var.min or var.max is not None and value > var.max:
            raise TemplateError(f"{var.label} must be between {var.min} and {var.max}")
        return value
    value = "" if raw is None else str(raw)
    if var.type == "select" and value not in var.options:
        raise TemplateError(f"{var.label} must be one of: {', '.join(var.options)}")
    if var.type == "text":
        if len(value) > 8000:
            raise TemplateError(f"{var.label} is too long")
        return value
    if "\n" in value or "\x00" in value:
        raise TemplateError(f"{var.label} must be a single line")
    if var.max is not None and len(value) > var.max:
        raise TemplateError(f"{var.label} must be at most {var.max} characters")
    if var.required and not value:
        raise TemplateError(f"{var.label} is required")
    if var.pattern and value and not re.fullmatch(var.pattern, value):
        raise TemplateError(f"{var.label} has an invalid value")
    return value


def defaults(template: Template) -> dict[str, Any]:
    return {v.key: v.default for v in template.variables}


def parse_extra_env(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise TemplateError(f"Environment line {n} must look like KEY=value")
        env[key] = value
    return env
