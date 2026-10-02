"""Entry-point stub: a scheduler started before the `sandbox/` move spawns `python -m istota.tool_server`."""
from istota.sandbox.tool_server import main

if __name__ == "__main__":
    raise SystemExit(main())
