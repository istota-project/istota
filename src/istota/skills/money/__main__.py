"""Allow running as `python -m istota.skills.money`.

One shape for all of them — `tests/test_skill_cli_facade.py` is the pin.
"""

from istota.skills._cli import run_skill_main

from . import main

run_skill_main(main)
