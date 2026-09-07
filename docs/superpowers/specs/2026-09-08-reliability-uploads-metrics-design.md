# Reliability, Upload Split, and Layout Metrics Design

## Scope

Implement only the approved reliability and upload changes plus anchored/unclipped metrics. Do not change solver N semantics, recipe multiplicity, or zone merging.

## Reliability

1. API memory: list/snapshot endpoints must query solution summary columns without materializing every `solutions.result` JSONB. Full result JSON is loaded only for a concrete solution/result. Increase API Kubernetes memory request/limit to 1Gi/4Gi.
2. Reinforcement capacity: inability to cover a load with the selected/catalog reinforcement is a domain-level infeasible analysis, not a worker crash. Persist `preparation_state=infeasible`, mark requested N values infeasible with a structured reason, publish an `analysis_infeasible` event, and return normally.
3. Fit retries: component/whole fit jobs are idempotent. If frontier already exists, resume downstream scheduling. If solver artifact disappeared but the prepared problem remains, enqueue solve again instead of raising `KeyError`.

## Upload API

- `POST /v1/tasks/upload`: DXF only; retains legacy `config + file` multipart contract.
- `POST /v1/tasks/tables_upload`: `config`, `nodes_file`, `elements_file`, `loads_file`, `load_column`.
- `POST /v1/tasks/json_upload`: `config`, one JSON file using source-polygons shape (list or `{polygons:[...]}`). Overlay response fields are ignored when creating the new source.
- `POST /v1/tasks/pickle_upload`: `config`, one restricted pickle. Only the NumPy/Shapely globals required by the supplied format are permitted.

All file formats are persisted as source bytes and materialized by a worker into canonical raw/smooth polygon JSON. `start=false` still materializes source polygons but does not prepare/solve. `start=true` materializes then prepares/solves.

## Layout metrics

A solution reports four reinforcement mass variants:

- without anchorage, clipped to physical field;
- with anchorage, clipped (the canonical `actual_mass_kg`);
- without anchorage, unclipped by field contours;
- with anchorage, unclipped by field contours.

Zone compatibility output must distinguish pre-anchorage and anchored geometry/bars instead of copying the same values. `unclipped` metrics ignore slab/overlay clipping only for metric calculation; they do not change solver feasibility or canonical physical layout.
