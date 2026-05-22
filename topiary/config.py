"""Config loading from TOML."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TabConfig:
    title: str
    command: str
    refresh: int = 5
    cwd: str | None = None


@dataclass
class PaneConfig:
    id: str
    type: str  # "command" | "system" | "tabs"
    title: str = ""
    width: str = "1fr"
    command: str | None = None
    refresh: int = 5
    min_width: int = 0  # collapse pane when terminal narrower than this
    cwd: str | None = None  # working directory for the command
    tabs: list[TabConfig] = field(default_factory=list)


@dataclass
class RowConfig:
    height: str = "1fr"
    panes: list[PaneConfig] = field(default_factory=list)


@dataclass
class AppConfig:
    title: str = "topiary"
    refresh_rate_hz: float = 20.0
    background: str = "transparent"
    rows: list[RowConfig] = field(default_factory=list)


def _parse_pane(raw: dict) -> PaneConfig:
    raw = dict(raw)
    tabs_raw = raw.pop("tabs", [])
    tabs = [TabConfig(**t) for t in tabs_raw]
    return PaneConfig(**raw, tabs=tabs)


def load_config(path: Path) -> AppConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    app_kw = {k: v for k, v in data.get("app", {}).items()}
    rows = []
    for row_raw in data.get("rows", []):
        row_raw = dict(row_raw)
        panes_raw = row_raw.pop("panes", [])
        panes = [_parse_pane(p) for p in panes_raw]
        rows.append(RowConfig(**row_raw, panes=panes))

    return AppConfig(**app_kw, rows=rows)
