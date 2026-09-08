"""Queue a metrics/layout refresh of SAVED solutions. Preview unless --apply.

Run inside a deployed API pod so normal PostgreSQL/Redis settings are used.
No solver/fit jobs are enqueued. Source polygons and component frontiers stay intact.
"""
from __future__ import annotations

import argparse
import json
from typing import Any
from uuid import uuid4

from .config import get_settings
from .layout_metrics import MASS_METRICS_VERSION
from .overlays import normalize_overlay_id
from .pipeline import PipelineJob
from .store import Store


def schedule_layout_refresh(
    store: Any, task_id: str, *, variant: str, overlay_id: int = 0,
    apply: bool = False, refresh_id: str | None = None, solution_id: str | None = None,
) -> dict[str, Any]:
    if variant not in {"raw", "smooth"}:
        raise ValueError("variant должен быть raw или smooth")
    overlay_id = normalize_overlay_id(overlay_id)
    meta = store.get_meta(task_id)
    if meta is None:
        raise ValueError(f"Задача не найдена: {task_id}")
    pending = int(store.pending_jobs(task_id))
    if apply and (meta.get("cancelled") or meta.get("paused") or pending):
        raise ValueError("Повторный layout разрешён для неотменённой задачи без паузы и активных jobs. Дождитесь завершения текущей обработки.")
    rows = store.solution_summaries(task_id, variant=variant, overlay_id=overlay_id)
    if solution_id is not None:
        rows = [row for row in rows if str(row["solution_id"]) == solution_id]
        if not rows:
            raise ValueError("Решение не найдено в указанном task/variant/overlay")
    batch = refresh_id or uuid4().hex
    report = {
        "task_id": task_id, "variant": variant, "overlay_id": overlay_id,
        "selected": len(rows), "queued": 0, "pending_before": pending,
        "apply": bool(apply), "refresh_id": batch, "metrics_schema_version": MASS_METRICS_VERSION,
    }
    if apply:
        generation = store.generation(task_id)
        for row in rows:
            payload = {
                "relayout_solution_id": str(row["solution_id"]),
                "total_n": int(row["total_N"]), "source": str(row["source"]),
                "variant": variant, "smooth": variant == "smooth", "overlay_id": overlay_id,
                "layout_refresh": batch, "metrics_version": MASS_METRICS_VERSION,
            }
            job = PipelineJob("layout_solution", task_id, payload, generation=generation)
            report["queued"] += int(store.enqueue_pipeline_job(job.to_dict()))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Повторный layout сохранённых решений без повторного solver. По умолчанию только просмотр.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--variant", choices=("raw", "smooth"), required=True)
    parser.add_argument("--overlay", type=int, default=0)
    parser.add_argument("--solution-id", default=None, help="Необязательно: только одно сохранённое решение")
    parser.add_argument("--run-id", default=None, help="Повтор с тем же run-id не создаёт дубликаты пока действует Redis dedupe")
    parser.add_argument("--apply", action="store_true", help="Поставить jobs в очередь; без флага ничего не изменяется")
    args = parser.parse_args()
    try:
        result = schedule_layout_refresh(
            Store(get_settings()), args.task_id, variant=args.variant, overlay_id=args.overlay,
            apply=args.apply, refresh_id=args.run_id, solution_id=args.solution_id,
        )
    except ValueError as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
