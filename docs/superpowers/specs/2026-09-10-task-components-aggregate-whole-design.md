# Task-scoped components and virtual whole aggregate

Дата: 2026-09-10

## Цель

Перенести семантику компонент из `scene` в `task`, потому что корректное разбиение на расчетные компоненты зависит от конфигурации армирования (`back_grid`, `stock`, `max_layers`, `load2cls`) и становится определенным только после выделения фонового армирования.

Одновременно убрать отдельную геометрическую solver-ветку `whole/-1`. `-1` должен быть виртуальным агрегатом результатов реальных компонент, а не отдельной геометрией, которая повторно проходит candidate-cover/MILP/solver.

`infeasible` для отдельного `N` является допустимым результатом конкретной попытки, а не ошибкой компоненты, task или analysis.

## 1. Жизненный цикл scene

`scene` хранит источник и подготовленные варианты полигонов:

1. API принимает DXF/XLSX/JSON/pickle.
2. API синхронно материализует source polygons.
3. API строит и сохраняет `raw` и `smooth` variants.
4. Scene сразу переходит в `ready` при успешной подготовке.
5. Новые scene **не создают расчетные компоненты**.

Таблица `scene_components` остается в схеме только для обратной совместимости со старыми данными/кодом. Для новых scene она не является источником истины и не заполняется.

`GET /v1/scenes/{scene_id}` не должен использовать `scene_components` для валидации будущего task.

## 2. Создание task и расчетных компонент

При создании нового task из готовой scene API имеет одновременно:

- выбранный scene variant (`raw`/`smooth`);
- overlay;
- task config, включая `back_grid`, `stock`, `max_layers`, `axis`, `anchor_factor`;
- component selection.

До постановки solver jobs API выполняет легкую подготовку:

1. получает resolved polygons для variant+overlay;
2. вызывает `resolve_rebar_config()`;
3. получает `load2cls` и определяет class `0` как background;
4. строит ортогональное представление demand;
5. вызывает `split_reinforcement_components()` уже с известным background;
6. сохраняет task-scoped `components` и field/decomposition;
7. только после этого ставит тяжелые jobs в общий Redis/worker pool.

Таким образом component IDs являются свойством **task analysis context**, а не scene.

## 3. Семантика реальных component IDs

Реальные расчетные компоненты имеют IDs `0..K-1` и создаются только из дополнительного demand после учета background.

Background-only polygons не создают solver component.

Явно запрошенный список component IDs валидируется после task decomposition, когда реальные IDs уже известны.

## 4. Семантика `-1` (whole)

`-1` больше не является отдельной физической/геометрической компонентой и не имеет собственного:

- `prepare_whole`;
- candidate-cover;
- `compute_max_n_whole`;
- `solve_whole`;
- `fit_whole`.

Для новых task `-1` — виртуальный агрегат frontier'ов реальных компонент.

Если реальных компонент несколько:

`aggregate.max_useful_n = sum(component_i.max_useful_n)`.

Несвязность компонент, отверстия и физические промежутки не влияют на существование агрегата, потому что каждая реальная компонента решается независимо.

Если реальная компонента ровно одна, `-1` является alias для component `0`. Никаких вторых prepare/max-N/solve/fit jobs не создается. В списке компонент отображается только `0`, без дублирующей строки `-1`.

## 5. Component selectors

Для новых task:

- `[-3]` — реальные компоненты, без публикации aggregate whole;
- `[-2]` — все реальные компоненты + виртуальный aggregate whole;
- `[-1]` — пользователь интересуется aggregate whole; внутри рассчитываются реальные компоненты, необходимые для агрегирования;
- `[0, 2, ...]` — только явно выбранные реальные компоненты.

При единственной компоненте запрос `-1` маршрутизируется на `0` как alias.

## 6. Планирование N для агрегата

Запрошенный aggregate total `T` нельзя планировать как `N=T` на каждой компоненте.

Для каждой реальной компоненты `i` известен `max_i`. Для положительного demand минимальный локальный N равен 1. Для requested total `T` локальный `n_i` может участвовать в агрегате только если:

`sum(n_i) = T` и `1 <= n_i <= max_i`.

Для component `i` допустимый диапазон для конкретного `T`:

- `low_i(T) = max(1, T - sum(max_j, j != i))`
- `high_i(T) = min(max_i, T - sum(1, j != i))`

Если `low_i(T) > high_i(T)`, aggregate `T` структурно недостижим для текущих max bounds.

Для нескольких requested totals API/планировщик берет объединение необходимых локальных диапазонов и ставит только эти `solve_component(n_i)` jobs. Порядок выполнения может оставаться edge-to-middle + round-robin.

Это гарантирует, что `combine_component_frontiers()` получает все локальные N, способные участвовать в requested aggregate totals, но не считает заведомо ненужные N.

## 7. Семантика infeasible

### 7.1 Отдельный component N

Если `solve_component(component_i, N)` возвращает infeasible:

- сохраняется результат/status только для пары `(component_i, N)`;
- component остается `prepared`;
- `max_useful_n` компоненты не обнуляется;
- `max_useful_n=0` зарезервирован только для анализа, где после учёта фона вообще нет реальных компонент; structural failure подготовки существующей компоненты хранит `max_useful_n=null`, а не `0`;
- остальные N этой же компоненты продолжают считаться;
- остальные компоненты продолжают считаться;
- результат не переводит task в `completed_with_errors`.

`infeasible` — валидный solver outcome, не исключение.

### 7.2 Aggregate total N

Aggregate frontier строится только из локальных результатов с `is_feasible=true`.

Если для requested total `T` нет ни одной комбинации feasible локальных результатов с суммой `T`:

- только aggregate status для `T` становится `infeasible`;
- другие aggregate totals продолжают искаться;
- реальные компоненты не получают состояние `infeasible` из-за отсутствия комбинации для `T`;
- analysis/task не считаются сломанными.

### 7.3 Когда analysis действительно infeasible

Analysis-level `infeasible` допустим только для условий, которые делают невозможным весь анализ как постановку, например глобальная `reinforcement_capacity` до component solver'ов или отсутствие любого положительного demand.

Обычный infeasible отдельного N не является analysis-level infeasible.

### 7.4 Ошибки против infeasible

`completed_with_errors`/`job_failed` используются только для исключений, повреждения данных, нарушения контрактов или инфраструктурных ошибок.

Корректно завершившийся solver с результатом infeasible не создает `job_failed`.

## 8. Комбинирование frontier'ов

`combine_component_frontiers()` продолжает min-plus комбинацию независимых frontier'ов и игнорирует строки `is_feasible=false`.

Комбинирование не должно требовать feasible результата для каждого одинакового N на каждой компоненте. Требуется только наличие feasible локальных вариантов, из которых можно собрать конкретный total.

Для одного component aggregate view переиспользует его frontier напрямую без копирования solver results.

## 9. API representation

`GET components` возвращает реальные task components.

Для K=1 возвращается только component `0`.

Aggregate/whole metadata для K>1 вычисляется виртуально:

- `id = -1` только на endpoint/view, где whole действительно запрошен;
- `max_useful_n = sum(real max_useful_n)` после подготовки max-N реальных компонент;
- state отражает готовность агрегирования, а не отдельный solver state.

`prepared=true` означает, что unit готов принимать/агрегировать N, а не что каждый возможный N feasible.

## 10. Backward compatibility

Старые worker handlers (`materialize_scene`, `materialize_source`, `prepare_whole`, `compute_max_n_whole`, `solve_whole`) не удаляются сразу, чтобы уже созданные старые jobs могли завершиться.

Новые scene/task flow их не enqueue'ят.

Существующие `scene_components` rows не мигрируются и не удаляются.

## 11. Тесты приемки

Нужны автоматические тесты, доказывающие:

1. scene upload создает `raw/smooth`, но не scene components;
2. task decomposition выполняется только после появления config/background;
3. background-only polygons не становятся solver components;
4. `-1` при K>1 не вызывает whole solver jobs;
5. `aggregate.max_useful_n == sum(real component max_useful_n)`;
6. при K=1 нет дублирования `0` и `-1`, а `-1` работает как alias;
7. infeasible `component 0, N=3` не мешает `N=2`/`N=4` и другим компонентам;
8. aggregate `T=10` может быть найден из, например, `3+7`, даже если локальные `N=10` infeasible/не существуют;
9. aggregate `T=11` infeasible не мешает получить aggregate `T=10` и `T=12`;
10. solver infeasible не генерирует `job_failed` и не переводит component в сломанное состояние;
11. legacy whole/materialize jobs по-прежнему dispatch'ятся для старых записей;
12. весь существующий test suite остается зеленым.

## 12. Не входит в изменение

- отдельный Redis для test API;
- отдельный worker pool;
- изменение KEDA/node autoscaling;
- миграция/удаление legacy `scene_components` данных;
- удаление legacy job kinds из worker protocol.
