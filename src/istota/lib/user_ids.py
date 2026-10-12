"""Case-insensitive user IDs retain their stored spelling."""

from collections.abc import Iterable


def user_id_index(user_ids: Iterable[str]) -> dict[str, str]:
    index: dict[str, str] = {}
    for user_id in user_ids:
        folded = user_id.casefold()
        existing = index.get(folded)
        if existing is not None and existing != user_id:
            raise ValueError(f"User ID {user_id!r} differs only in case from {existing!r}")
        index[folded] = user_id
    return index
