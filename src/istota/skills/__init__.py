"""Skills modules for istota - wrappers for external tools."""

# Star imports kept deliberately: this package re-exports each library-only
# skill's whole surface, and enumerating the names here would be a second list
# to keep in step with three modules. F403 only reports that ruff cannot see
# through them, which is the point of the form.
from .calendar import *  # noqa: F403
from .email import *  # noqa: F403
from .files import *  # noqa: F403
