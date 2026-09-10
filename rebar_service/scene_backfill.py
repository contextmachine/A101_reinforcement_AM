"""Schedule worker-side component materialization for legacy scenes; dry-run by default."""
from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import text


def backfill_scenes(store: Any, workflow: Any, *, limit: int = 100, apply: bool = False) -> dict[str, Any]:
    if not 1 <= int(limit) <= 10000:
        raise ValueError("limit must be between 1 and 10000")
    with store.database.connect() as conn:
        ids = list(conn.execute(text("""
            SELECT id FROM scenes
            WHERE COALESCE((metadata->>'needs_component_backfill')::boolean, false)
            ORDER BY created_at, id LIMIT :limit
        """), {"limit": int(limit)}).scalars().all())
    queued = 0
    if apply:
        for scene_id in ids:
            queued += int(bool(workflow.enqueue_scene_materialization(str(scene_id))))
    return {"selected": len(ids), "queued": queued, "apply": bool(apply), "scene_ids": ids}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--apply", action="store_true", help="Actually enqueue scene jobs. Default is read-only preview.")
    args = parser.parse_args()
    from .config import get_settings
    from .store import Store
    from .pipeline import PipelineWorkflow
    settings = get_settings()
    store = Store(settings)
    result = backfill_scenes(store, PipelineWorkflow(store, settings), limit=args.limit, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
