"""Istota agent runtime — the generic agent loop and its supporting types.

This package is the brain-agnostic layer between the provider abstraction
(``istota.llm``) and the application layer (``istota.session``). It knows
nothing about skills, Nextcloud, or sandboxing — only about driving a
tool-use loop against an ``LLMProvider`` and emitting lifecycle events.

Phase 0 of the agent-loop migration seeds this package with the
tool-description renderer (``events._describe_tool_use``) shared between the
Claude Code stream parser and the future native loop. Phases 2+ add the loop,
tool protocol, hooks, and full ``AgentEvent`` lifecycle types.
"""
