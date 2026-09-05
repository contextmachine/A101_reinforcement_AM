# A101 reinforcement optimizer

Сервис расчёта дополнительного армирования: один FastAPI API, один Redis-store, один тип worker и один актуальный component-aware pipeline.

`run.py`, `run2.py`, `run3.py` — только локальные тестовые сценарии. Kubernetes их не запускает.

## Production architecture

```text
client/frontend
      |
      v
rebar_service.api:app
      |
      +-- old frontend-compatible endpoints
      +-- component / solution endpoints
      |
      v
RedisStore
  rebar:jobs:ready
  rebar:jobs:processing
  rebar:jobs:workload
      |
      v
rebar_service.worker
      |
      v
PipelineWorkflow
  prepare_field
  prepare_component
  solve_component
  fit_component
  combine_frontiers
  layout_solution
  validate_solution (optional)
  prepare_whole / solve_whole / fit_whole (optional)
      |
      +--> /v1/tasks/{id}/solutions
      +--> /v1/tasks/{id}/results/{total_N}
```

`/results/{total_N}` сохраняет формат, который использовал существующий frontend, но данные берутся из того же актуального solution, что и `/solutions`.

## Component semantics

`n: [1, 2, 3]` означает кандидаты `N=1,2,3` **для каждой компоненты отдельно**. Для компоненты считаются только значения `N <= max_useful_n`.

Если все запрошенные `N` больше `max_useful_n`, для этой компоненты создаётся fallback `N=1` — один прямоугольник, покрывающий компоненту.

После расчёта component frontiers они комбинируются. Поэтому итоговый `total_N` обычно не равен одному из исходных `N`:

```text
component 0 -> N=1
component 1 -> N=2
component 2 -> N=1
------------------
total_N = 4
```

`scan_mode=hard` перебирает `1..max_useful_n` для каждой компоненты. `whole=true` дополнительно запускает расчёт всего поля как одной компоненты.

## API

Swagger:

```text
https://rebar.contextmachine.cloud/docs
```

### Создание задачи

`POST /v1/tasks/upload` — multipart upload DXF/JSON.

Поле `config` — **JSON-строка**, например:

```json
{
  "n": [1, 2, 3],
  "back_grid": [18, 300],
  "stock": [[18, 300], [20, 150], [20, 100], [25, 150], [25, 100]],
  "max_layers": 2,
  "axis": "y",
  "anchor_factor": 32,
  "min_width_mm": 1000,
  "solver": {
    "backend": "highs",
    "threads": 1,
    "timeout_seconds": null,
    "solver_time_limit": null
  }
}
```

Минимальный smoke-test:

```json
{"n":[1,2,3]}
```

Пример curl:

```bash
curl -X POST 'https://rebar.contextmachine.cloud/v1/tasks/upload' \
  -F 'config={"n":[1,2,3]}' \
  -F 'file=@drawing.dxf'
```

Дополнительные query-параметры upload:

- `scan_mode=requested|hard`
- `whole=true|false`
- `component_result_top_k=5`
- `validate_results=true|false`

### Frontend-compatible endpoints

Сохранены старые URL и основные response shapes:

```text
POST /v1/tasks
POST /v1/tasks/upload
GET  /v1/tasks/{task_id}
GET  /v1/tasks/{task_id}/source-polygons
GET  /v1/tasks/{task_id}/results
GET  /v1/tasks/{task_id}/results/{total_N}
GET  /v1/tasks/{task_id}/results/{total_N}/dxf
GET  /v1/tasks/{task_id}/events
POST /v1/tasks/{task_id}/n
POST /v1/tasks/{task_id}/cancel
POST /v1/tasks/{task_id}/pause
POST /v1/tasks/{task_id}/resume
WS   /v1/tasks/{task_id}/ws
```

### Component / solution endpoints

```text
GET  /v1/tasks/{task_id}/components
GET  /v1/tasks/{task_id}/components/{component_id}
POST /v1/tasks/{task_id}/components/{component_id}/n
GET  /v1/tasks/{task_id}/components/{component_id}/results
GET  /v1/tasks/{task_id}/components/{component_id}/results/{n}
POST /v1/tasks/{task_id}/components/prepare
GET  /v1/tasks/{task_id}/component-events
GET  /v1/tasks/{task_id}/solutions
GET  /v1/tasks/{task_id}/solutions/{solution_id}
```

Для `POST .../components/{component_id}/n` значения выше `max_useful_n` отклоняются с HTTP 422.

## Local `run3.py`

`run3.py` не запускает API и worker. Это локальный запуск текущей схемы без Redis:

```bash
python run3.py drawing.dxf -n 1 2 3
python run3.py drawing.dxf -n 1 2 3 --hard-scan
python run3.py drawing.dxf -n 1 2 3 --whole
```

По умолчанию solver/fit не имеют ограничения по времени. При необходимости его можно задать явно:

```bash
python run3.py drawing.dxf -n 1 2 3 --timeout 3600 --time-limit 3500
```

Результаты пишутся в `run3_output/run3_result.pkl` и `run3_output/run3_summary.json`.

## Production entrypoints

API:

```bash
python -m uvicorn rebar_service.api:app --host 0.0.0.0 --port 8000
```

Worker:

```bash
python -m rebar_service.worker
```

Docker image по умолчанию стартует API. Worker Deployment переопределяет command.

## Redis

Используется одна физическая семья очередей:

```text
rebar:jobs:ready
rebar:jobs:processing
rebar:jobs:workload
```

`workload` хранит все незавершённые jobs до `ack`, поэтому KEDA видит не только ожидающие, но и уже выполняющиеся работы.

`REBAR_JOB_LEASE_SECONDS` — не ограничение времени вычисления. Worker продлевает lease heartbeat-ом; lease нужен для возврата job после реального падения Pod.

## Solver time limits

В Kubernetes по умолчанию:

```text
REBAR_SOLVER_TIMEOUT=none
REBAR_SOLVER_TIME_LIMIT=none
REBAR_FIT_TIME_LIMIT=none
```

`none`, `null`, `off` и `unlimited` интерпретируются как отсутствие лимита. Явный timeout можно передать в task config.

Потоки solver:

```text
REBAR_DEFAULT_SOLVER_THREADS=1
REBAR_MAX_SOLVER_THREADS=4
```

Количество Pod и количество solver threads независимы.

## Kubernetes

Namespace:

```text
rebar-optimizer
```

Основные manifests:

```text
deploy/k8s/base/api.yaml
deploy/k8s/base/worker-deployment.yaml
deploy/k8s/base/configmap.yaml
deploy/k8s/overlays/prod/worker-scaledobject.yaml
deploy/k8s/overlays/prod/ingress.yaml
```

KEDA 2.20.2:

```bash
helm upgrade --install keda kedacore/keda \
  --namespace keda \
  --create-namespace \
  --version 2.20.2
```

Manifests рассчитаны на Kubernetes 1.33–1.35.

KEDA смотрит только на `rebar:jobs:workload` и масштабирует Deployment `rebar-worker` от 0.

Redis для KEDA указывается полным cluster DNS:

```text
rebar-redis.rebar-optimizer.svc.cluster.local:6379
```

## Image / branch

Рабочий image:

```text
ghcr.io/contextmachine/a101_reinforcement_am:am-super-branch
```

`.github/workflows/ci.yml` запускается на каждый push в `am-super-branch` и публикует branch tag + sha tag. `latest` публикуется только для default branch.

Оба Kustomize overlay используют:

```yaml
newTag: am-super-branch
```

После новой сборки branch tag:

```bash
kubectl -n rebar-optimizer rollout restart deployment/rebar-api
kubectl -n rebar-optimizer rollout restart deployment/rebar-worker
```

`imagePullPolicy: Always` заставляет новый Pod проверить актуальный digest плавающего branch tag.

## Production ingress

```text
https://rebar.contextmachine.cloud
```

Ingress class: `nginx`; TLS secret: `rebar-api-tls`; cert-manager issuer: `letsencrypt`.

Проверка:

```bash
curl https://rebar.contextmachine.cloud/health/live
curl https://rebar.contextmachine.cloud/openapi.json
```

## Verification

```bash
python -m compileall -q A101 rebar_service run3.py
pytest -q
python run3.py --help
```

Render manifests:

```bash
kubectl kustomize deploy/k8s/overlays/prod > rendered-prod.yaml
kubectl apply --dry-run=server -f rendered-prod.yaml
```

Проверить image:

```bash
grep 'image:' rendered-prod.yaml
```

Ожидается `ghcr.io/contextmachine/a101_reinforcement_am:am-super-branch`.
