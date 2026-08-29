"""Op-based USER.md curation.

Public surface:

- `parse_sectioned_doc(text) -> SectionedDoc`
- `serialize_sectioned_doc(doc) -> str`
- `apply_ops(doc, ops) -> (new_doc, applied, rejected)`
- `build_op_curation_prompt(user_id, doc, dated_memories, kg_facts_text, skill_overlays=None)`
- `strip_json_fences(text) -> str`
- `write_audit_log(config, user_id, applied, rejected)`
- `read_audit_entries(config, user_id, limit=None)`
- `migrate_user_md_sidecars(config, user_id)`
"""

from .audit import migrate_user_md_sidecars, read_audit_entries, write_audit_log
from .ops import apply_ops, apply_ops_with_db
from .parser import parse_sectioned_doc, serialize_sectioned_doc
from .prompt import build_op_curation_prompt, strip_json_fences
from .types import Section, SectionedDoc

__all__ = [
    "Section",
    "SectionedDoc",
    "apply_ops",
    "apply_ops_with_db",
    "build_op_curation_prompt",
    "migrate_user_md_sidecars",
    "parse_sectioned_doc",
    "read_audit_entries",
    "serialize_sectioned_doc",
    "strip_json_fences",
    "write_audit_log",
]
