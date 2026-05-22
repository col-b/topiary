"""Entry point: topiary [config.toml]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .app import TopiaryApp
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="topiary",
        description="A configurable TUI dashboard",
    )
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        help="Path to TOML config file (default: ./topiary.toml or ~/.config/topiary/topiary.toml)",
    )
    args = parser.parse_args()

    config_path = _resolve_config(args.config)
    if config_path is None:
        parser.error(
            "No config file found. Pass a path, or create topiary.toml in the current "
            "directory or ~/.config/topiary/topiary.toml"
        )

    try:
        config = load_config(config_path)
    except Exception as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        sys.exit(1)

    app = TopiaryApp(config)
    app.run()


def _resolve_config(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit

    candidates = [
        Path("topiary.toml"),
        Path.home() / ".config" / "topiary" / "topiary.toml",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


if __name__ == "__main__":
    main()
