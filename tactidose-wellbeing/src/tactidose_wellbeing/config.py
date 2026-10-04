"""Runtime settings (environment variables) and safety/support configuration (JSON file)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from .domain.safety import (
    DEFAULT_HANDOFF_MESSAGE,
    DEFAULT_NO_RESOURCES_MESSAGE,
    DEFAULT_URGENT_MESSAGE,
    DEFAULT_URGENT_PHRASES,
    CrisisResource,
    SafetyConfig,
)

ENV_PREFIX = "TACTIDOSE_WELLBEING_"
AuthMode = Literal["unconfigured", "dev", "static_tokens"]


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/wellbeing.sqlite3"
    # Default is "unconfigured": every user-scoped endpoint refuses requests
    # until a real identity mechanism is configured (fail safe).
    auth_mode: AuthMode = "unconfigured"
    api_tokens_file: str | None = None
    config_file: str | None = None
    session_ttl_seconds: int = 1800
    host: str = "127.0.0.1"
    port: int = 8080

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env

        def get(name: str, default: str | None = None) -> str | None:
            return env.get(ENV_PREFIX + name, default)

        mode = get("AUTH_MODE", "unconfigured")
        if mode not in ("unconfigured", "dev", "static_tokens"):
            raise ValueError(f"Unknown {ENV_PREFIX}AUTH_MODE: {mode!r}")
        return cls(
            db_path=get("DB_PATH", cls.db_path),
            auth_mode=mode,  # type: ignore[arg-type]
            api_tokens_file=get("API_TOKENS_FILE"),
            config_file=get("CONFIG_FILE"),
            session_ttl_seconds=int(get("SESSION_TTL_SECONDS", str(cls.session_ttl_seconds))),
            host=get("HOST", cls.host),
            port=int(get("PORT", str(cls.port))),
        )


def load_safety_config(path: str | Path | None) -> SafetyConfig:
    """Load urgent-support wording and crisis resources from a JSON file.

    No crisis contact is built in: resources must be supplied and verified by
    the deploying team for their region.
    """
    if not path:
        return SafetyConfig()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    resources = tuple(
        CrisisResource(name=str(r["name"]), contact=str(r["contact"]), notes=r.get("notes"))
        for r in data.get("crisis_resources", [])
    )
    return SafetyConfig(
        urgent_phrases=tuple(data.get("urgent_phrases", DEFAULT_URGENT_PHRASES)),
        urgent_message=data.get("urgent_support_message", DEFAULT_URGENT_MESSAGE),
        no_resources_message=data.get("no_resources_message", DEFAULT_NO_RESOURCES_MESSAGE),
        crisis_resources=resources,
        support_handoff_message=data.get("support_handoff_message", DEFAULT_HANDOFF_MESSAGE),
    )
