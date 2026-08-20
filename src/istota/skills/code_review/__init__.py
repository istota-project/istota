"""Code review skill.

The CLI facade (`build_parser`, `main`, `cmd_run`) and the model call land in a
later stage; today this package holds `engine.py`, which is everything the
review does *without* a model — range resolution, hardened git invocation, diff
and context assembly, sizing, prompt assembly, and finding parsing and merging.

There is deliberately no `__main__.py` and no `cli: true` in `skill.md` yet.
`cli_skills` is computed straight off the index with no filter beyond that flag,
and dispatch is `python -m istota.skills.<name>`, so advertising the module
before it can run would put a `ModuleNotFoundError` behind a menu entry.
"""
