"""Centralised logging for topiary → ~/.logs/topiary.log

Usage anywhere in the package:

    from .log import log
    log.info("something happened")
    log.getChild("pane.btop").debug("detail")
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_LOG_PATH = Path.home() / ".logs" / "topiary.log"
_FMT = "%(asctime)s %(levelname)-8s %(name)-24s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup() -> logging.Logger:
    """Initialise rotating file logging.  Call once at startup in __main__."""
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        _LOG_PATH,
        maxBytes=1 * 1024 * 1024,   # 1 MB per file
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))

    logger = logging.getLogger("topiary")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        logger.addHandler(handler)

    # Keep noisy third-party loggers quiet
    logging.getLogger("textual").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    return logger


# Module-level logger — safe to import before setup() is called.
log = logging.getLogger("topiary")
