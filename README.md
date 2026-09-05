# A101 reinforcement optimizer

Сервис расчёта дополнительного армирования: FastAPI API, PostgreSQL как долговременное хранилище, Redis только как очередь/KEDA coordination и один тип worker с component-aware pipeline.

`run.py`, `run2.py`, `run3.py` — локальные тестовые сценарии. Kubernetes их не запускает.

## Production architecture

```text
client/frontend
      |
      v
rebar_service.api:app
      |
      +-----------------------------+
      |                             |
      v                             v
PostgreSQL                       Redis
(tasks, variants,              ready / processing
components, results,           workload / leases
solutions, events)             dedupe / slots
      ^                             |
      |                             v
      +------ rebar_service.worker -+
                    |
                    v
             PipelineWorkflow
```

PostgreSQL — единственный durable source of truth. Redis можно полностью очистить без потери завершённых задач и результатов; после очистки теряются только незавершённые jobs очереди.

`/results/{total_N}` сохраняет frontend-compatible response shape, но отдельная legacy-копия результата не хранится: ответ строится из canonical `solutions`.

## PostgreSQL storage

Используется PostgreSQL 16 через Kubernetes Service:

```text
host: a101-postgres
port: 5432
database: a101
namespace: rebar-optimizer
```

API, worker и migration Job получают username/password из уже существующего Secret `a101-postgres-auth`, keys `POSTGRES_USER` и `POSTGRES_PASSWORD`. Значения credentials в репозиторий не записываются.

Основные таблицы:

```text
tasks
├── task_sources
├── task_variants          # raw + smooth polygon JSONB
├── task_n_requests        # N/status per variant
├── components
│   └── component_results  # frontier per component/N
├── runtime_artifacts      # internal pickle+zstd BYTEA checkpoints
├── solutions              # canonical final results
└── task_events            # durable ordered events
```

`task_variants` всегда содержит отдельные `raw` и `smooth` записи. Smooth рассчитывается один раз при создании задачи и больше не пересчитывается worker-ом.

Данные задач, raw/smooth JSON, компоненты, frontier, solutions, исходный DXF и events хранятся бессрочно. Временные `solver_result` и `candidate` artifacts удаляются после успешного потребления. Field/component/problem checkpoints остаются, чтобы к старой задаче можно было позже добавить новые N.

Schema migrations выполняет Alembic. Initial migration:

```text
migrations/versions/0001_postgres_storage.py
```

## Redis

Redis содержит только ephemeral queue/coordination state:

```text
rebar:jobs:ready          # full job JSON
rebar:jobs:processing     # full claimed job JSON
rebar:jobs:workload       # job_id only; KEDA uses LLEN
rebar:job:<id>
rebar:job:<id>:lease
rebar:job-dedupe:<hash>
rebar:task:<id>:pending
rebar:task:<id>:slots
rebar:lock:queue-reaper
```

В Redis больше нет task metadata, polygons, blobs, frontiers, solutions, results, events, generation или cancellation state.

`REBAR_JOB_LEASE_SECONDS` — не ограничение времени вычисления. Worker продлевает lease heartbeat-ом; lease нужен для возврата job после падения Pod.

## Component semantics

`n: [1, 2, 3]` означает кандидаты `N=1,2,3` для каждой компоненты отдельно. Для компоненты считаются только значения `N <= max_useful_n`.

Если все запрошенные N больше `max_useful_n`, для этой компоненты создаётся fallback `N=1` — один прямоугольник, покрывающий компоненту.

После расчёта component frontiers они комбинируются, поэтому итоговый `total_N` обычно не равен одному из исходных N:

```text
component 0 -> N=1
component 1 -> N=2
component 2 -> N=1
------------------
total_N = 4
```

`scan_mode=hard` перебирает `1..max_useful_n` для каждой компоненты. `whole=true` дополнительно запускает расчёт всего поля как одной компоненты.

Raw/smooth имеют независимые `task_n_requests`, component rows и results.

## API

Swagger:

```text
https://rebar.contextmachine.cloud/docs
```

### Создание задачи

`POST /v1/tasks/upload` — multipart upload DXF/JSON/XLSX source.

Поле `config` — JSON-строка, например:

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

```bash
curl -X POST 'https://rebar.contextmachine.cloud/v1/tasks/upload' \
  -F 'config={"n":[1,2,3]}' \
  -F 'file=@drawing.dxf'
```

Query-параметры upload:

- `scan_mode=requested|hard`
- `whole=true|false`
- `component_result_top_k=5`
- `validate_results=true|false`
- `smooth=true|false`

Persisted source polygons:

```text
GET /v1/tasks/{task_id}/source-polygons?smooth=false
GET /v1/tasks/{task_id}/source-polygons?smooth=true
```

### Frontend-compatible endpoints

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

## Local `run3.py`

`run3.py` не запускает API/worker/PostgreSQL/Redis:

```bash
python run3.py drawing.dxf -n 1 2 3
python run3.py drawing.dxf -n 1 2 3 --hard-scan
python run3.py drawing.dxf -n 1 2 3 --whole
```

## Production entrypoints

API:

```bash
python -m uvicorn rebar_service.api:app --host 0.0.0.0 --port 8000
```

Worker:

```bash
python -m rebar_service.worker
```

Migration:

```bash
alembic upgrade head
```

## Solver time limits

В Kubernetes по умолчанию:

```text
REBAR_SOLVER_TIMEOUT=none
REBAR_SOLVER_TIME_LIMIT=none
REBAR_FIT_TIME_LIMIT=none
```

`none`, `null`, `off` и `unlimited` интерпретируются как отсутствие лимита.

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
deploy/k8s/base/db-migrate-job.yaml
deploy/k8s/overlays/prod/worker-scaledobject.yaml
```

KEDA 2.20.2:

```bash
helm upgrade --install keda kedacore/keda \
  --namespace keda \
  --create-namespace \
  --version 2.20.2
```

Manifests рассчитаны на Kubernetes 1.33–1.35. KEDA смотрит только на `rebar:jobs:workload`.

## First clean cutover: Redis -> PostgreSQL

Этот переход намеренно не мигрирует старые Redis tasks. Скрипт сначала проверяет PostgreSQL и успешно применяет Alembic migration, затем останавливает старый API/worker, удаляет KEDA ScaledObject на время перехода, выполняет `FLUSHDB` и применяет новую версию. Поэтому необратимый шаг происходит только после успешной миграции схемы.

Сначала соберите/push-ните новый image, содержащий этот код. Затем:

```bash
export IMAGE='ghcr.io/contextmachine/a101_reinforcement_am:<new-tag>'
CONFIRM_REDIS_FLUSH=YES \
  ./scripts/cutover-postgres.sh prod "$IMAGE" rebar.contextmachine.cloud
```

`CONFIRM_REDIS_FLUSH=YES` обязателен специально: `FLUSHDB` необратимо удаляет текущие Redis keys. Скрипт использует только выбранную Redis DB и не выполняет `FLUSHALL`.

Если нужно выполнить шаги отдельно:

```bash
export IMAGE='ghcr.io/contextmachine/a101_reinforcement_am:<new-tag>'

./scripts/migrate-db.sh "$IMAGE"

kubectl -n rebar-optimizer scale deployment/rebar-api --replicas=0
kubectl -n rebar-optimizer delete scaledobject rebar-worker --ignore-not-found=true
kubectl -n rebar-optimizer scale deployment/rebar-worker --replicas=0

./scripts/clear-redis.sh
./scripts/deploy-k8s.sh prod "$IMAGE" rebar.contextmachine.cloud
```

### Redis credential rotation

Предыдущий dev manifest содержал Redis password в репозитории. Файл удалён и заменён на `deploy/k8s/secrets/rebar-secrets.dev.example.yaml`. Считайте старое значение скомпрометированным и смените Redis credential.

Для Redis StatefulSet, который читает `rebar-secrets`, можно создать новый Secret без вывода пароля на экран:

```bash
NEW_REDIS_PASSWORD="$(openssl rand -hex 32)"
kubectl -n rebar-optimizer create secret generic rebar-secrets \
  --from-literal=REBAR_REDIS_PASSWORD="$NEW_REDIS_PASSWORD" \
  --from-literal=REBAR_REDIS_URL="redis://:${NEW_REDIS_PASSWORD}@rebar-redis:6379/0" \
  --from-literal=REBAR_REDIS_ADDRESS='rebar-redis:6379' \
  --dry-run=client -o yaml | kubectl apply -f -
unset NEW_REDIS_PASSWORD
```

После обновления Secret перезапустите workload Redis, который задаёт `--requirepass`, а API/worker будут перезапущены новым deployment. Если production Redis управляется отдельным manifest/operator, обновите credential тем же значением там до запуска API/worker.

## Database verification after cutover

Проверить Alembic revision и таблицы без раскрытия PostgreSQL credentials:

```bash
kubectl -n rebar-optimizer exec deploy/a101-postgres -- sh -ec \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT version_num FROM alembic_version;"'

kubectl -n rebar-optimizer exec deploy/a101-postgres -- sh -ec \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\\dt"'
```

После создания первой новой задачи проверить raw/smooth:

```bash
kubectl -n rebar-optimizer exec deploy/a101-postgres -- sh -ec \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT task_id, variant, jsonb_array_length(polygons) AS polygons FROM task_variants ORDER BY task_id, variant;"'
```

Проверить, что canonical solutions появились:

```bash
kubectl -n rebar-optimizer exec deploy/a101-postgres -- sh -ec \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT task_id, variant, total_n, status, actual_mass_kg FROM solutions ORDER BY created_at DESC LIMIT 20;"'
```

Проверить Redis queue keys:

```bash
REDIS_POD="$(kubectl -n rebar-optimizer get pod -l app=rebar-redis -o jsonpath='{.items[0].metadata.name}')"
kubectl -n rebar-optimizer exec "$REDIS_POD" -c redis -- sh -ec \
  'redis-cli -a "$REBAR_REDIS_PASSWORD" --no-auth-warning --scan'
```

После cutover до постановки новых jobs список должен быть пустым. После работы допустимы только queue/lease/dedupe/pending/slots/reaper key families, описанные выше.

## Normal deployment after cutover

Для будущих версий с migrations сначала выполняйте migration Job из нового image, затем deploy:

```bash
export IMAGE='ghcr.io/contextmachine/a101_reinforcement_am:<tag>'
./scripts/migrate-db.sh "$IMAGE"
./scripts/deploy-k8s.sh prod "$IMAGE" rebar.contextmachine.cloud
```

Alembic migrations должны быть backward-compatible с ещё работающей предыдущей версией API/worker, если deploy выполняется без downtime.

## Verification

Локально:

```bash
./scripts/verify.sh
```

Внутри скрипта выполняются:

```text
python -m compileall
pytest
alembic upgrade head --sql
kubectl kustomize ...   # если kubectl установлен
```

Production readiness:

```bash
kubectl -n rebar-optimizer rollout status deployment/rebar-api --timeout=5m
kubectl -n rebar-optimizer get deployment/rebar-worker
kubectl -n rebar-optimizer get scaledobject/rebar-worker
curl https://rebar.contextmachine.cloud/health/live
curl https://rebar.contextmachine.cloud/health/ready
```
