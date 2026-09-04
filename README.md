# Rebar Optimizer API

Сервис оборачивает вычислительный pipeline проекта в FastAPI + Redis + распределённые workers. Один тяжёлый этап подготовки создаёт `prepared`, после чего разные `N` решаются параллельно и обмениваются лучшими incumbents через Redis.

## Архитектура

```text
client
  ├─ POST /v1/tasks
  ├─ GET  /v1/tasks/{id}
  └─ WS   /v1/tasks/{id}/ws
             │
             ▼
          FastAPI
             │
             ▼
           Redis
      ┌──────┼────────────┐
      │      │            │
    queues  events     blobs/data
      │
      ▼
worker Deployment ← KEDA ScaledObject
      │
      ├─ prepare task → prepared/context
      └─ solve task   → N result/incumbents/data
```

Workers — обычный `Deployment`, а не `ScaledJob`. KEDA масштабирует его от 0 до заданного лимита. После опустошения очереди pods остаются тёплыми ещё 300 секунд (`cooldownPeriod`). Worker сам по idle timeout не завершается.

### Почему три Redis-очереди

```text
rebar:jobs:ready       ожидают worker
rebar:jobs:processing  уже взяты worker
rebar:jobs:workload    все незавершённые jobs
```

`ready → processing` нужен для надёжного claim/recovery. `workload` создаётся отдельно для autoscaling: запись находится в нём от постановки job до `ack_job()`. Поэтому долгий solver остаётся видим KEDA даже после удаления из `ready`.

## Что поддерживается

`N` может быть:

```json
{"n": 30}
```

```json
{"n": [10, 20, 30, 40]}
```

или адаптивным диапазоном:

```json
{"n": {"start": 1, "stop": 100, "coarse_step": 10}}
```

Для диапазона сначала идут крупные точки, затем промежуточные и только потом оставшиеся значения. Через WebSocket можно добавлять новые `N`, отменять отдельные `N`, ставить диапазон на паузу и получать incumbents/финальные решения.

---

# 1. Локальный запуск

Требования:

- Docker + Docker Compose;
- для запуска без Docker — Python 3.12 и Redis.

Создай `.env`:

```bash
cp .env.example .env
```

Для локальной машины дефолты уже согласованы с `docker-compose.yml`:

```dotenv
REBAR_REDIS_PASSWORD=dev-password
REBAR_API_KEY=dev-api-key
REBAR_MAX_JOBS_PER_TASK=4
REBAR_GLOBAL_MAX_JOBS=32
```

Запуск API + Redis + 4 workers:

```bash
docker compose up --build --scale worker=4
```

Проверка:

```bash
curl http://localhost:8000/health/ready
```

Swagger:

```text
http://localhost:8000/docs
```

Остановка:

```bash
docker compose down
```

Удалить локальные данные Redis:

```bash
docker compose down -v
```

---

# 2. Создание задачи

## JSON с полигонами

Пример находится в `examples/task_polygons.json`.

```bash
curl -X POST http://localhost:8000/v1/tasks \
  -H 'content-type: application/json' \
  -H 'x-api-key: dev-api-key' \
  --data-binary @examples/task_polygons.json
```

## DXF

Основная конфигурация находится в `examples/task_config.json`.

```bash
curl -X POST http://localhost:8000/v1/tasks/upload \
  -H 'x-api-key: dev-api-key' \
  -F 'config=<examples/task_config.json' \
  -F 'file=@/path/to/input.dxf'
```

После POST вернётся `task_id`.

---

# 3. HTTP API

| Method | URL | Назначение |
|---|---|---|
| POST | `/v1/tasks` | JSON задача |
| POST | `/v1/tasks/upload` | DXF/JSON upload |
| GET | `/v1/tasks/{id}` | snapshot статуса |
| GET | `/v1/tasks/{id}/events` | журнал событий |
| GET | `/v1/tasks/{id}/results` | метаданные результатов |
| GET | `/v1/tasks/{id}/results/{N}` | полный результат N |
| POST | `/v1/tasks/{id}/n` | добавить/retry N |
| POST | `/v1/tasks/{id}/cancel` | отменить N или задачу |
| POST | `/v1/tasks/{id}/pause` | пауза диапазона |
| POST | `/v1/tasks/{id}/resume` | продолжить диапазон |

Добавить N:

```bash
curl -X POST http://localhost:8000/v1/tasks/TASK_ID/n \
  -H 'content-type: application/json' \
  -H 'x-api-key: dev-api-key' \
  -d '{"n":[35,45]}'
```

Отменить N:

```bash
curl -X POST http://localhost:8000/v1/tasks/TASK_ID/cancel \
  -H 'content-type: application/json' \
  -H 'x-api-key: dev-api-key' \
  -d '{"n":[45]}'
```

---

# 4. WebSocket

```text
ws://localhost:8000/v1/tasks/TASK_ID/ws?token=dev-api-key&after=0-0
```

Основные события:

```text
preparation_started
preparation_progress
prepared
n_queued
n_started
solver_heartbeat
incumbent
n_finished
n_error
task_state
```

Команды клиента:

```json
{"action":"add","n":[35,45]}
{"action":"cancel","n":[45]}
{"action":"pause_range"}
{"action":"resume_range"}
{"action":"snapshot"}
{"action":"cancel_task"}
```

Готовый клиент:

```bash
python examples/ws_client.py TASK_ID
```

---

# 5. Параллелизм

На одну пользовательскую задачу ограничение выполняется приложением через Redis semaphore:

```dotenv
REBAR_MAX_JOBS_PER_TASK=4
```

Пользователь может запросить меньше через `max_concurrent_jobs`, но не должен обходить серверный предел.

Глобальный предел в Kubernetes задаётся в `worker-scaledobject.yaml`:

```yaml
maxReplicaCount: 32
```

и для документации/валидации дублируется:

```dotenv
REBAR_GLOBAL_MAX_JOBS=32
```

Для большого количества независимых `N` обычно используйте `solver.threads=1` и больше worker pods, а не много потоков внутри одного solver.

---

# 6. Kubernetes: первый запуск dev

Используется Kustomize `base + overlays`: общий base не знает об окружении, а dev/prod добавляют свои ресурсы и patches. Это стандартная модель Kustomize.

## 6.1 Установить KEDA один раз

Для KEDA 2.20 в этом комплекте ориентируйтесь на Kubernetes 1.33–1.35. Если кластер другой версии, сначала сверяйте матрицу совместимости вашей версии KEDA.

```bash
helm repo add kedacore https://kedacore.github.io/charts
helm repo update
helm install keda kedacore/keda \
  --version 2.20.2 \
  --namespace keda \
  --create-namespace
```

## 6.2 Создать namespace и secret

```bash
kubectl apply -f deploy/k8s/base/namespace.yaml
cp deploy/k8s/secrets/rebar-secrets.dev.example.yaml /tmp/rebar-secrets.yaml
```

Измени пароли в `/tmp/rebar-secrets.yaml`, затем:

```bash
kubectl apply -f /tmp/rebar-secrets.yaml
```

Secret в git не коммить.

## 6.3 Указать image

Для ручного запуска проще сначала отрендерить overlay и заменить placeholder:

```bash
IMAGE=ghcr.io/MY_ORG/MY_REPO:latest
kubectl kustomize deploy/k8s/overlays/dev \
  | sed "s#ghcr.io/contextmachine/a101_reinforcement_am:am-super-branch#$IMAGE#g" \
  | kubectl apply -f -
```

Проверка:

```bash
kubectl -n rebar-optimizer get pods,deploy,statefulset,svc
kubectl -n rebar-optimizer get scaledobject,hpa
kubectl -n rebar-optimizer describe scaledobject rebar-worker
```

В dev overlay встроен Redis StatefulSet с PVC.

Проброс API:

```bash
kubectl -n rebar-optimizer port-forward svc/rebar-api 8000:80
```

После этого API доступен на `http://localhost:8000`.

---

# 7. Kubernetes: production

Prod overlay **не создаёт Redis**. Предполагается внешний/managed Redis.

Создай secret по шаблону:

```bash
kubectl apply -f deploy/k8s/base/namespace.yaml
cp deploy/k8s/secrets/rebar-secrets.prod.example.yaml /tmp/rebar-secrets.yaml
```

Заполни:

```text
REBAR_REDIS_PASSWORD
REBAR_REDIS_URL
REBAR_REDIS_ADDRESS
REBAR_API_KEY
```

и примени:

```bash
kubectl apply -f /tmp/rebar-secrets.yaml
```

Для публичного GHCR image:

```bash
IMAGE=ghcr.io/MY_ORG/MY_REPO:sha-abc123
HOST=rebar.my-domain.ru
kubectl kustomize deploy/k8s/overlays/prod \
  | sed "s#ghcr.io/contextmachine/a101_reinforcement_am:am-super-branch#$IMAGE#g; s#rebar.example.com#$HOST#g" \
  | kubectl apply -f -
```

То же самое короче через готовый скрипт:

```bash
./scripts/deploy-k8s.sh prod ghcr.io/MY_ORG/MY_REPO:sha-abc123 rebar.my-domain.ru
```

Для private GHCR сначала:

```bash
kubectl -n rebar-optimizer create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io \
  --docker-username=GITHUB_USER \
  --docker-password=GITHUB_TOKEN
```

и используй:

```bash
kubectl kustomize deploy/k8s/overlays/prod-private ...
```

Ingress в prod рассчитан на nginx ingress controller. Если у вас другой controller, меняются `ingressClassName` и annotations в `deploy/k8s/overlays/prod/ingress.yaml`.

---

# 8. Как работает autoscaling

`ScaledObject` следит за:

```text
rebar:jobs:workload
```

При `listLength: "1"` один незавершённый job соответствует примерно одной требуемой worker replica, но итог ограничен `maxReplicaCount`.

Когда workload становится нулевым, KEDA ждёт:

```yaml
cooldownPeriod: 300
```

перед scale-to-zero. Поэтому workers остаются тёплыми примерно пять минут.

Дополнительно HPA scale-down имеет `stabilizationWindowSeconds: 300`. Если Kubernetes всё же посылает worker `SIGTERM`, worker больше не берёт новые jobs, завершает уже выполняющийся job и только затем выходит. `terminationGracePeriodSeconds` в Deployment установлен чуть выше максимального разрешённого solver timeout.

---

# 9. GitHub Actions

`ci.yml`:

1. устанавливает зависимости;
2. запускает compile/test;
3. после успешного push собирает image;
4. публикует GHCR tags `branch`, `tag`, `sha`, `latest` для main.

Для GHCR repository GitHub Actions использует `GITHUB_TOKEN`.

`deploy.yml` запускается вручную. Нужен GitHub Secret:

```text
KUBE_CONFIG_B64
```

Linux:

```bash
base64 -w0 ~/.kube/config
```

Создай GitHub Environments:

```text
dev
prod
prod-private
```

При `workflow_dispatch` выбери:

```text
environment = dev | prod | prod-private
image_tag   = sha-... | v... | latest
ingress_host
```

Workflow сам рендерит нужный overlay, подставляет GHCR image/host и применяет manifests. Одновременные deploy одного environment запрещены через `concurrency`.

Kubernetes secret `rebar-secrets` и KEDA должны быть созданы заранее.

---

# 10. Что удалить из предыдущей версии

Полный список находится в `MIGRATION.md`.

Главное удалить старые:

```text
deploy/k8s/06-worker-scaledjob.yaml
deploy/k8s/kustomization.yaml
deploy/k8s-private/
```

и вообще не оставлять одновременно:

```text
ScaledJob/rebar-worker
ScaledObject/rebar-worker
```

Новый Kubernetes tree:

```text
deploy/k8s/
├── base/
│   ├── namespace.yaml
│   ├── configmap.yaml
│   ├── api.yaml
│   ├── worker-deployment.yaml
│   └── kustomization.yaml
├── overlays/
│   ├── dev/
│   ├── prod/
│   └── prod-private/
└── secrets/
```

---

# 11. TTL и тяжёлые данные

Сейчас Redis хранит queue/state/events/solver data и chunked blobs (`input`, `prepared`, context, results). TTL задачи:

```dotenv
REBAR_TASK_TTL_SECONDS=172800
```

то есть 48 часов.

Для первого production этого достаточно. Если `prepared`/results станут большими и Redis RAM окажется узким местом, следующий логичный шаг — оставить в Redis queue/state/incumbents, а тяжёлые blobs вынести в S3/MinIO. API-контракт для этого менять не требуется.

---

# 12. Полезные команды

Логи API:

```bash
kubectl -n rebar-optimizer logs -l app=rebar-api -f --max-log-requests=10
```

Логи workers:

```bash
kubectl -n rebar-optimizer logs -l app=rebar-worker -f --max-log-requests=32
```

Текущее масштабирование:

```bash
kubectl -n rebar-optimizer get deploy rebar-worker
kubectl -n rebar-optimizer get hpa
kubectl -n rebar-optimizer describe scaledobject rebar-worker
```

Длина workload при встроенном dev Redis:

```bash
kubectl -n rebar-optimizer exec statefulset/rebar-redis -- \
  sh -c 'redis-cli -a "$REBAR_REDIS_PASSWORD" LLEN rebar:jobs:workload'
```

Удаление dev окружения:

```bash
kubectl delete -k deploy/k8s/overlays/dev
```

PVC Redis удаляется отдельно, если данные больше не нужны.

---

# 13. Проверки перед production

```bash
python -m compileall -q A101 rebar_service
pytest
```

Затем обязательно сделать staging smoke test:

1. POST маленькой задачи;
2. убедиться, что worker Deployment поднимается с 0;
3. увидеть `prepared`;
4. запустить несколько `N`;
5. проверить WebSocket incumbents;
6. дождаться пустого workload;
7. убедиться, что примерно через пять минут workers масштабировались в 0;
8. проверить восстановление job после принудительного удаления worker pod.
