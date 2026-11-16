"""Agent configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentConfig:
    token: str
    data_dir: Path
    state_dir: Path
    sample_interval: float
    history_size: int
    # Where data_dir's parent lives as the Docker daemon sees it (e.g. D:\Hearth on Windows shows
    # up as /run/desktop/mnt/host/d/Hearth). None: detect it from the agent's own mounts.
    host_root: Path | None = None

    @classmethod
    def from_env(cls) -> "AgentConfig":
        token = os.environ.get("HEARTH_AGENT_TOKEN", "")
        if len(token) < 32:
            raise RuntimeError(
                "HEARTH_AGENT_TOKEN must be set to the token shown in the panel (32+ characters)."
            )
        # Where server data lives inside this container. Its location on the host (any drive) is
        # detected at startup, or can be given with HEARTH_HOST_DATA_DIR.
        root = Path(os.environ.get("HEARTH_DATA_DIR", "/var/lib/hearth"))
        host_root = os.environ.get("HEARTH_HOST_DATA_DIR")
        return cls(
            token=token,
            data_dir=root / "servers",
            state_dir=root / "agent",
            sample_interval=float(os.environ.get("HEARTH_SAMPLE_INTERVAL", "10")),
            history_size=int(os.environ.get("HEARTH_HISTORY_SIZE", "60")),
            host_root=Path(host_root) if host_root else None,
        )
