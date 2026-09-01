"""Configuration file I/O — accepts plain TOML or UPPERCASE.md with TOML blocks.

The UPPERCASE.md pattern matches istota's CRON.md/HEARTBEAT.md convention:
prose explaining the config to the user, with a fenced ```toml code block
holding the actual configuration. The first toml block is parsed.
"""

from pathlib import Path

import tomli

# Where the fence starts and ends is `toml_fence`'s to say (ISSUE-386). The
# expression that used to live here anchored neither marker, so a backtick
# run anywhere after the fence opened ended the block early.
from ..toml_fence import find_toml_block


def read_toml_config(path: Path) -> dict:
    """Read a config file as TOML.

    If ``path`` ends in ``.md`` (case-insensitive), the file is treated as
    markdown and the first ```toml fenced block is extracted. Files ending
    in ``.toml`` (or any other suffix) are parsed as plain TOML.

    Raises ValueError if a markdown file has no toml code block.
    """
    text = path.read_text()
    if path.suffix.lower() == ".md":
        span = find_toml_block(text)
        if span is None:
            raise ValueError(
                f"No ```toml code block found in {path}; expected an "
                f"UPPERCASE.md-style config with the TOML body fenced."
            )
        text = text[span[0]:span[1]]
    return tomli.loads(text)
