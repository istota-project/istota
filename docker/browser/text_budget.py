"""Character windows shared by plain text and markdown responses."""

MAX_TEXT_CHARS = 500000


def checked_offset(value):
    """Accept only a nonnegative integer character offset."""
    if type(value) is not int or value < 0:
        raise ValueError("offset must be a nonnegative integer")
    return value


def text_window(text, max_chars, offset=0):
    """Return an exact slice and metadata outside the payload."""
    offset = checked_offset(offset)
    budget = max(1, min(max_chars, MAX_TEXT_CHARS))
    total = len(text)
    end = min(offset + budget, total)
    metadata = {}
    if end < total or offset:
        metadata["text_total_chars"] = total
        metadata["text_offset"] = offset
    if end < total:
        metadata["text_truncated"] = True
        metadata["text_truncated_by"] = (
            "text_ceiling" if budget == MAX_TEXT_CHARS else "max_chars"
        )
        metadata["next_offset"] = end
    return text[offset:end], metadata
