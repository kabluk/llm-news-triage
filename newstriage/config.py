"""Configuration: config/sources.yaml, config/rules.yaml, config/settings.yaml and env vars.

rules.yaml (what matters and how it is scored) and settings.yaml (when and how
the digest goes out) are merged into one dict, ``Config.rules``; their top-level
keys must not overlap."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines; never overrides variables already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0]
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_rules(config_dir: Path) -> dict:
    rules = yaml.safe_load((config_dir / "rules.yaml").read_text(encoding="utf-8")) or {}
    settings = yaml.safe_load((config_dir / "settings.yaml").read_text(encoding="utf-8")) or {}
    overlap = set(rules) & set(settings)
    if overlap:
        raise ValueError(f"keys defined in both rules.yaml and settings.yaml: {sorted(overlap)}")
    return {**rules, **settings}


@dataclass
class Config:
    sources: list[dict]
    rules: dict
    root: Path
    data_dir: Path
    out_dir: Path
    env: dict = field(default_factory=dict)
    config_dir: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "newstriage.db"

    def source(self, sid: str) -> dict | None:
        return next((s for s in self.sources if s["id"] == sid), None)


def load_config(config_dir: Path | None = None, data_dir: Path | None = None,
                out_dir: Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    config_dir = Path(config_dir or os.environ.get("NEWSTRIAGE_CONFIG_DIR") or ROOT / "config")
    data_dir = Path(data_dir or os.environ.get("NEWSTRIAGE_DATA_DIR") or ROOT / "data")
    out_dir = Path(out_dir or os.environ.get("NEWSTRIAGE_OUT_DIR") or ROOT / "out")
    data_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = yaml.safe_load((config_dir / "sources.yaml").read_text(encoding="utf-8"))["sources"]
    ids = [s["id"] for s in sources]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate source id in sources.yaml")
    return Config(sources=sources, rules=load_rules(config_dir), root=ROOT, data_dir=data_dir,
                  out_dir=out_dir, env=dict(os.environ), config_dir=config_dir)
