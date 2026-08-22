"""Staging environment for the deployment tiers.

Not part of the shipped application and not importable from it: `src/istota/`
must never reach in here, and nothing here may be needed to run the daemon.
It sits beside `src/` rather than inside `tests/` because two consumers outside
this repo — istota-demo and istota-redteam — already proved the plumbing
generalizes by copying it twice, and an installable package is what ends that.

The three pieces, and the seam between them:

- `stack.py` drives a compose stack (`Stack`) and the `docker compose`
  invocation underneath it.
- `services/` holds everything the daemon talks to that is not the daemon,
  behind one `Service` protocol: a stub we wrote, or a real server we run.
- `probe.py` reads the framework DB back, from a host path or through
  `docker compose exec`.

Nothing here imports pytest. A test module is welcome to call `pytest.fail` on
what these raise; a package the external rigs install must not need a test
runner to be present.
"""
