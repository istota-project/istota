"""Token estimation — chars/4 heuristic.

Good enough for compaction threshold checks. Conservative (overestimates),
which is the right direction: trigger compaction a little early rather than
overflow the context window. Prior art: Pi's estimateTokens().
"""

import json

# Flat per-image token estimate. Vision content costs roughly this regardless
# of the base64 payload length, so counting the base64 chars would wildly
# overestimate. ~4800 chars / 4 ≈ 1200 tokens per image.
_IMAGE_TOKEN_CHARS = 4800


def estimate_tokens(message) -> int:
    """Estimate the token count of a single message via chars/4.

    Walks the message's content blocks, accumulating a character count per
    block type, then divides by 4 (floor 1). Handles text, thinking, tool-call
    (name + JSON-serialized arguments), and image (flat estimate) blocks.
    """
    chars = 0
    for block in getattr(message, "content", []):
        text = getattr(block, "text", None)
        thinking = getattr(block, "thinking", None)
        arguments = getattr(block, "arguments", None)
        data = getattr(block, "data", None)

        if text is not None:
            chars += len(text)
        elif thinking is not None:
            chars += len(thinking)
        elif arguments is not None:
            chars += len(getattr(block, "name", "")) + len(json.dumps(arguments))
        elif data is not None:
            chars += _IMAGE_TOKEN_CHARS

    return max(1, chars // 4)
