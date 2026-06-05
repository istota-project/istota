"""Istota session layer — application-level concerns for the native brain.

The session layer sits above the generic agent runtime (``istota.agent``) and
the provider abstraction (``istota.llm``). It owns result composition, prompt
assembly, context compaction, retry classification, and the ``NativeBrain``
adapter to the ``Brain`` protocol.

Phase 0 of the agent-loop migration extracts brain-agnostic helpers here
(result composition, malformed-output detection) so the executor monolith
shrinks without any behavior change. The executor re-exports these symbols for
backward compatibility.
"""
