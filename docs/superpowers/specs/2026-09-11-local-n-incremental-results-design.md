# Local-N Scheduling and Incremental Results Design

## Goal

Interpret every user-supplied `n` as a local solver N for each selected real component and/or the direct whole-field solver, then expose every aggregate total N that can be built from feasible component frontiers. Preserve prior results across later N additions and continuously improve the best result per total N.

## Selection semantics

- `[-3]`: prepare/solve all real components for requested local N values; continuously combine their feasible frontiers into aggregate field solutions. Do not run direct whole-field solve.
- `[-1]`: run only the direct whole-field solver for requested local N values when there are multiple real components. With exactly one real component, reuse that component as the whole-field alias and do not create duplicate whole jobs.
- `[-2]`: run both paths: all real components plus aggregate combinations, and direct whole-field solves. With exactly one real component, reuse component 0 and do not duplicate the whole calculation.
- Explicit non-negative component IDs: solve those components for requested local N values. If more than one is selected, their feasible frontiers may be combined.

## Local N rules

For each selected solver unit, schedule each requested N independently, capped by that unit's computed `max_useful_n`. Values above the unit maximum are skipped for that unit rather than turning the task or aggregate into infeasible.

A later `/n` request adds local N values to the existing plan. Existing completed local `(component, n)` results are retained and not recomputed. New values are scheduled only where missing and within each unit's maximum.

## Aggregate results

Component combination totals are outputs, not inputs. They MUST NOT be filtered by the user-supplied local N list.

As soon as every selected real component has at least one feasible frontier result, run min-plus combination over all currently feasible component results. Every reachable `total_N` is eligible for a solution. A later local result may create a new `total_N` or improve an existing one.

Local infeasible results are simply omitted from combination options. They do not mark the component, aggregate, analysis, or task as broken.

## Direct whole-field results

The direct whole-field solver remains independent from component aggregation. It is prepared and solved only for selectors `[-1]` and `[-2]`. Its input local N values are the exact user-requested values. The direct whole-field admissible range starts at `N=1` regardless of the number of real components, because one whole-field zone may span multiple component demand regions through traversable background. Its upper useful bound is the sum of the real-component `max_useful_n` values (subject to the server per-solver hard limit when scheduling).

For `[-1]` with multiple components, real components are prepared only far enough to obtain their max-N bounds; their `solve_component` jobs are not scheduled. When the final component max-N becomes available, the waiting whole-field bound is finalized and the requested whole N jobs are queued.

Whole-field and component-aggregate solutions may compete for the same `total_N`.

## Best-so-far behavior

All feasible solutions may be retained, but the public result for a fixed `total_N` is always the currently best solution ordered primarily by actual mass. A newly discovered lower-mass solution replaces the previous best response immediately. Worse recomputation of the same stable solution identity must never overwrite a better persisted result.

Publish solution availability as soon as layout finishes; when the best solution for a total changes, publish a result update event. Do not wait for all local N jobs to finish before exposing a feasible result.

## Request plan versus result totals

`task_n_requests` remains the set of user-requested local N values. Aggregate output totals must not be inserted into this request plan, because doing so would cause later scheduler passes to treat aggregate totals as new local inputs. Aggregate totals are represented by persisted `solutions` / `results` and result events.

## Completion semantics

A task with no pending jobs and no execution failures is `completed`, even if some or all local solver N values are infeasible. `completed_with_errors` is reserved for actual pipeline/job failures.

## Compatibility

Keep legacy stored-whole handlers and old task behavior where required. New scene-backed tasks use the selection semantics above. No database schema migration is required for this change.
