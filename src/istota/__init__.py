"""Istota - Claude Code-powered bot with Nextcloud Talk interface."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("istota")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
