"""Memory subsystem.

Public surface re-exported here for back-compat and to document the
shape of the package. In-repo callers do explicit
`from istota.memory.search import ...` or
`from istota.memory.knowledge_graph import ...` rather than
relying on these aliases.
"""

from .search import (
    MemoryChunk,
    SearchResult,
    _delete_source_chunks,
    enable_vec_extension,
    ensure_vec_table,
    get_stats,
    index_conversation,
    index_file,
    reindex_all,
)
# NOTE: the `search` function is intentionally NOT re-exported here — it
# would shadow the submodule and break `from istota.memory import search as ...`
# patterns. Import it explicitly: `from istota.memory.search import search`.
from .knowledge_graph import (
    KnowledgeFact,
    add_fact,
    delete_fact,
    ensure_table,
    format_facts_for_prompt,
    get_current_facts,
    get_entity_timeline,
    get_fact,
    get_fact_count,
    get_facts_as_of,
    invalidate_fact,
    select_relevant_facts,
)

__all__ = [
    "MemoryChunk",
    "SearchResult",
    "_delete_source_chunks",
    "enable_vec_extension",
    "ensure_vec_table",
    "get_stats",
    "index_conversation",
    "index_file",
    "reindex_all",
    "KnowledgeFact",
    "add_fact",
    "delete_fact",
    "ensure_table",
    "format_facts_for_prompt",
    "get_current_facts",
    "get_entity_timeline",
    "get_fact",
    "get_fact_count",
    "get_facts_as_of",
    "invalidate_fact",
    "select_relevant_facts",
]
