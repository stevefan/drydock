"""Workspace domain model."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class Workspace:
    name: str
    project: str
    repo_path: str
    id: str = ""
    worktree_path: str = ""
    branch: str = ""
    base_ref: str = "HEAD"
    state: str = "defined"
    container_id: str = ""
    # Persisted as a first-class field rather than inside config dict so registry queries can filter on it.
    workspace_subdir: str = ""
    image: str = ""
    owner: str = ""
    created_at: str = ""
    updated_at: str = ""
    config: dict = field(default_factory=dict)
    # Phase 0 (project-dock-ontology.md): SHA-256 of the project YAML
    # at the moment this Drydock's policy was last pinned (create or
    # `ws project reload`). Empty string = never pinned (legacy row).
    pinned_yaml_sha256: str = ""
    # Phase Y0 (yard.md): optional Yard membership. None = standalone.
    # FK to yards.id; not enforced at the SQLite level (SQLite FKs need
    # PRAGMA foreign_keys=ON which we don't currently set), but enforced
    # in code by checking yard existence at create.
    yard_id: str | None = None

    def __post_init__(self):
        if not self.id:
            # Name is unique in the registry (UNIQUE constraint), so it's a
            # sufficient identifier on its own. Project is metadata, not part
            # of the id.
            name_slug = self.name.replace("-", "_").replace(" ", "_")
            self.id = f"ws_{name_slug}"
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> dict:
        return asdict(self)
