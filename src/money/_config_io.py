"""Configuration file I/O — accepts plain TOML or UPPERCASE.md with TOML blocks.

The UPPERCASE.md pattern matches istota's CRON.md/BRIEFINGS.md convention:
prose explaining the config to the user, with a fenced ```toml code block
holding the actual configuration. The first toml block is parsed.
"""

import re
from pathlib import Path

import tomli

_TOML_BLOCK_RE = re.compile(r"```toml\s*\n(.*?)```", re.DOTALL)


def read_toml_config(path: Path) -> dict:
    """Read a config file as TOML.

    If ``path`` ends in ``.md`` (case-insensitive), the file is treated as
    markdown and the first ```toml fenced block is extracted. Files ending
    in ``.toml`` (or any other suffix) are parsed as plain TOML.

    Raises ValueError if a markdown file has no toml code block.
    """
    text = path.read_text()
    if path.suffix.lower() == ".md":
        match = _TOML_BLOCK_RE.search(text)
        if not match:
            raise ValueError(
                f"No ```toml code block found in {path}; expected an "
                f"UPPERCASE.md-style config with the TOML body fenced."
            )
        text = match.group(1)
    return tomli.loads(text)
