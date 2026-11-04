from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
from typing import Callable

LOGGER_NAME = "steamshotbridge"


def default_log_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "SteamShotBridge" / "logs"


def setup_logging(
    log_dir: Path | str | None = None,
    ui_error_callback: Callable[[str, str], None] | None = None,
) -> logging.Logger:
    directory = Path(log_dir) if log_dir is not None else default_log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / "app.log"

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)

    # Evita adicionar múltiplos handlers idênticos
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            logger.removeHandler(handler)

    handler = RotatingFileHandler(
        str(log_file),
        maxBytes=1024 * 1024,  # 1 MB
        backupCount=3,
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    def excepthook_handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        logger.error(
            "Exceção não tratada no sistema:",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        if ui_error_callback is not None:
            try:
                ui_error_callback("erro", f"Erro inesperado: {exc_value}")
            except Exception:
                pass

    sys.excepthook = excepthook_handler

    def thread_excepthook_handler(args):
        if issubclass(args.exc_type, KeyboardInterrupt):
            return

        logger.error(
            f"Exceção não tratada na thread {args.thread.name}:",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        if ui_error_callback is not None:
            try:
                ui_error_callback("erro", f"Erro inesperado: {args.exc_value}")
            except Exception:
                pass

    if hasattr(threading, "excepthook"):
        threading.excepthook = thread_excepthook_handler

    return logger
