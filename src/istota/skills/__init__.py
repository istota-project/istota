"""Skills modules for istota - wrappers for external tools."""

import os as _os

# Every skill CLI enters this package before its own module. The proxy passes
# this fd through one exec only; tools the skill later execs must not inherit it.
if "ISTOTA_CRED_FD" in _os.environ:
    try:
        _os.set_inheritable(int(_os.environ["ISTOTA_CRED_FD"]), False)
    except (OSError, ValueError, OverflowError):
        # Resolution reports the invalid channel as a refusal before dispatch.
        pass

# Star imports kept deliberately: this package re-exports each library-only
# skill's whole surface, and enumerating the names here would be a second list
# to keep in step with three modules. F403 only reports that ruff cannot see
# through them, which is the point of the form.
from .calendar import *  # noqa: F403
from .email import *  # noqa: F403
from .files import *  # noqa: F403
