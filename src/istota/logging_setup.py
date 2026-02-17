"""Configuration loading for istota."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config

# Track whether logging has been initialized to prevent double-init
_initialized = False


def setup_logging(
    config: Config,
    verbose: bool = False,
    daemon_mode: bool = False,
) -> None:
    """
    Configure logging for the istota application.

    Args:
        config: Application configuration with logging settings
        verbose: If True, override config level to DEBUG
        daemon_mode: If True, include timestamps in console output
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    log_config = config.logging

    # Determine log level
    level_str = "DEBUG" if verbose else log_config.level.upper()
    level = getattr(logging, level_str, logging.INFO)

    # Configure root logger for istota namespace
    logger = logging.getLogger("istota")
    logger.setLevel(level)
    logger.handlers.clear()

    # Console format - timestamps for daemon mode, simpler for CLI
    if daemon_mode:
        console_format = "%(asctime)s %(levelname)-5s [%(name)-18s] %(message)s"
        date_format = "%Y-%m-%d %H:%M:%S"
    else:
        console_format = "%(levelname)-5s [%(name)-18s] %(message)s"
        date_format = None

    # Add console handler if output includes console
    if log_config.output in ("console", "both"):
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(level)
        console_handler.setFormatter(logging.Formatter(console_format, datefmt=date_format))
        logger.addHandler(console_handler)

    # Add file handler if output includes file
    if log_config.output in ("file", "both") and log_config.file:
        file_path = Path(log_config.file)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if log_config.rotate:
            file_handler = RotatingFileHandler(
                file_path,
                maxBytes=log_config.max_size_mb * 1024 * 1024,
                backupCount=log_config.backup_count,
            )
        else:
            file_handler = logging.FileHandler(file_path)

        file_format = "%(asctime)s %(levelname)-5s [%(name)-18s] %(message)s"
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(file_format, datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(file_handler)

    # Suppress noisy third-party loggers
    for noisy_logger in ("httpx", "httpcore", "caldav", "urllib3"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def reset_logging() -> None:
    """Reset logging state for testing purposes."""
    global _initialized
    _initialized = False
    logger = logging.getLogger("istota")
    logger.handlers.clear()
