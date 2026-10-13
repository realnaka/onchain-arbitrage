from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OKXConfig:
    api_key: str
    secret_key: str
    passphrase: str
    simulated: bool = True

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "OKXConfig":
        env = environ or os.environ
        return cls(env.get("OKX_API_KEY", ""), env.get("OKX_SECRET_KEY", ""),
                   env.get("OKX_PASSPHRASE", ""), env.get("OKX_SIMULATED", "1") == "1")

    def validate(self) -> None:
        if not self.simulated:
            raise ValueError("live OKX is disabled by project policy")
        missing = [k for k, v in (("OKX_API_KEY", self.api_key),
                    ("OKX_SECRET_KEY", self.secret_key), ("OKX_PASSPHRASE", self.passphrase)) if not v]
        if missing:
            raise ValueError("missing OKX demo credentials: " + ", ".join(missing))


def load_env_file(path: str | Path) -> dict[str, str]:
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip("'\"")
    return result
