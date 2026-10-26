"""Allow running as `python -m istota.skills.whisper`.

One shape for all of them — `tests/test_skill_cli_facade.py` is the pin.
"""

from istota.skills._cli import run_skill_main

from .cli import main

run_skill_main(main)
