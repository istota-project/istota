"""Type definitions for the skill plugin system."""

from dataclasses import dataclass, field


@dataclass
class EnvSpec:
    """Declarative env var wiring specification.

    Defined in skill.toml [[env]] sections. Each spec describes how to
    resolve one environment variable for the Claude subprocess.
    """

    var: str
    source: str  # "config", "resource", "resource_json", "user_resource_config", "template_file"
    # For source="config"
    config_path: str = ""
    when: str = ""  # guard: dotted config path that must be truthy
    # For source="resource" / "resource_json"
    resource_type: str = ""
    # For source="user_resource_config"
    field: str = ""
    # For source="template_file"
    template: str = ""
    user_path_fn: str = ""


@dataclass
class SkillMeta:
    """Metadata for a skill, loaded from skill.toml or _index.toml."""

    name: str
    description: str
    always_include: bool = False
    admin_only: bool = False
    keywords: list[str] = field(default_factory=list)
    resource_types: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    file_types: list[str] = field(default_factory=list)
    env_specs: list[EnvSpec] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    # Where the skill was loaded from (for doc/code resolution)
    skill_dir: str = ""
