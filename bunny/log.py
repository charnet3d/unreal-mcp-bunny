"""Logging setup: console + rotating file in <root>/logs.

Root dir: BUNNY_ROOT_DIR env, else the folder containing the bunny package.
Point BUNNY_ROOT_DIR at a deployment folder to keep config/data/logs out of
the source tree (side-by-side instances stay independent).

Level: BUNNY_LOG_LEVEL env (DEBUG shows every client POST — the trace you
want when a harness handshake dies), default INFO.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

_ROOT = (Path(os.environ["BUNNY_ROOT_DIR"]).resolve()
         if os.environ.get("BUNNY_ROOT_DIR")
         else Path(__file__).resolve().parent.parent)
_LOG_DIR = _ROOT / "logs"
_configured = False


def setup(level: str | None = None) -> None:
    global _configured
    if _configured:
        return
    level = (level or os.environ.get("BUNNY_LOG_LEVEL") or "INFO").upper()
    _LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger("bunny")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        fh = logging.handlers.RotatingFileHandler(
            _LOG_DIR / "bunny.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup()
    return logging.getLogger("bunny." + name)
