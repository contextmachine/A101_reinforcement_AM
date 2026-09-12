"""Move v2 artifacts stored inline in Postgres to the file backend (REBAR_ARTIFACT_DIR).

One artifact at a time, so the database never has to serve more than one large bytea.
Run inside the cluster with the artifact volume mounted:
    python -m rebar_service.v2.artifacts_migrate
"""

from __future__ import annotations

import sys
from typing import Callable


def migrate_inline_artifacts(v2, *, log: Callable[[str], None] = print) -> int:
    if getattr(v2, "artifact_backend", None) is None:
        raise RuntimeError("REBAR_ARTIFACT_DIR is not configured; nothing to migrate to")
    moved = 0
    for task_id, key in v2.inline_artifact_keys():
        value = v2.load_artifact(task_id, key)
        if value is None:
            continue
        v2.save_artifact(task_id, key, value)
        moved += 1
        log(f"moved {task_id} {key}")
    log(f"migrated {moved} artifact(s)")
    return moved


def main() -> int:
    from ..config import get_settings
    from ..store import Store

    store = Store(get_settings())
    migrate_inline_artifacts(store.v2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
