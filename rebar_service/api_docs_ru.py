"""Russian Swagger/OpenAPI descriptions. No changes to request validation or routing."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

CONFIG_EXAMPLE = {
    "n": {"start": 1, "stop": 20, "coarse_step": 1},
    "max_layers": 2, "min_width_mm": 300,
    "steel_density_kg_m3": 7850, "anchor_factor": 40, "axis": "x",
}

LEGACY_OPERATION_GROUPS = [
    {"name": "1. Загрузка", "description": "Создание задач из DXF, таблиц XLSX, JSON или pickle. Форматы разделены по ручкам."},
    {"name": "2. Задача и полигоны", "description": "Состояние задачи и исходные полигоны raw/smooth."},
    {"name": "3. Оверлеи", "description": "Неизменяемый журнал исключения/возврата полигонов. Ревизия выбирается через overlay."},
    {"name": "4. Запуск и управление", "description": "Первый запуск сам подготавливает анализ; пауза, продолжение и отмена."},
    {"name": "5. Компоненты", "description": "Task-scoped декомпозиция и промежуточные результаты реальных компонентов. Для новых task -1 — виртуальный агрегат, а при одной компоненте alias на 0."},
    {"name": "6. Решения и экспорт", "description": "Финальные раскладки, совместимый со старым фронтом формат results и DXF."},
    {"name": "7. События", "description": "Прогресс и причины ошибок. events — курсор по id, component-events — смещение OFFSET."},
    {"name": "8. Состояние сервиса", "description": "Проверки процесса API и доступности хранилищ."},
]

V2_TAGS = [
    {"name": "1. V2 — Загрузка", "description": "Создание чистых v2-сцен из DXF, JSON и XLSX."},
    {"name": "2. V2 — Сцены и overlays", "description": "Полигоны сцены и append-only overlays."},
    {"name": "3. V2 — Задачи", "description": "Whole-field pipeline: создать задачу, добавить/отменить N и читать состояние."},
    {"name": "4. V2 — Bars и verification", "description": "Асинхронная раскладка стержней и проверка армирования."},
]
SERVICE_TAG = {"name": "5. Состояние сервиса", "description": "Проверки процесса API и доступности PostgreSQL/Redis."}
V2_INTRO = "Минималистичный whole-field API v2. Legacy `/v1` скрыт из обычного Swagger и доступен отдельно через `/docs/legacy`."

INTRO = """## Рекомендуемый сценарий работы
1. Создайте reusable-сцену через `POST /v1/scenes/dxf_upload`, `json_upload`, `tables_upload` или `pkl_upload`. API синхронно разбирает источник и строит raw/smooth; расчётные компоненты на уровне scene не создаются. Успешный ответ содержит `scene_id` и `state=ready`.
2. После успешной загрузки scene сразу готова к чтению полигонов, overlays и созданию анализа через `PUT /v1/tasks`. Отдельного ожидания worker только ради разбора исходника нет.
3. При необходимости добавьте append-only overlays сцены. Получить геометрию состояния можно через `/v1/scenes/{scene_id}/polygons?smooth=...&overlay=...`.
4. Запустите анализ через `PUT /v1/tasks`: передайте `scene_id`, `smooth`, `overlay_id`, конфигурацию, список `n` и выбор компонент. Ответ содержит новый `task_id` и фактический `overlay_id`.
5. После запуска task является **неизменяемым (immutable) снимком**: `scene_id`, raw/smooth и разрешённый overlay больше не меняются. Результаты читаются по `task_id` и N; для отдельной компоненты дополнительно используется её индекс.

### Совместимость старого upload/front-end
Существующие `/v1/tasks/upload`, `/tables_upload`, `/json_upload`, `/pickle_upload` сохранены. Формат multipart старого `/v1/tasks/upload` (`config` + `file`) не меняется; в ответ дополнительно добавлен `scene_id`. Для `/v1/tasks/upload` `whole=true` по умолчанию, но явно переданное `whole=false` сохраняет прежний смысл.
Старые ручки чтения результатов по-прежнему принимают query-параметры `smooth` и `overlay`. Для legacy-задач их default — raw (`smooth=false`) + `overlay=0`. Для новых immutable-task сохранённый контекст task является авторитетным: конфликтующие legacy query-параметры его не переопределяют.

### Overlay
Overlay принадлежит **scene**, а не task. `overlay=0` означает исходные полигоны. Положительное значение — точный существующий непрозрачный id события; числовое значение id не обязано совпадать с его порядком.
Отрицательные селекторы считаются по append-order: `overlay=-1` — последнее событие, `-2` — предпоследнее и т.д. При создании task отрицательный селектор немедленно разрешается; task хранит и во всех ответах возвращает уже реальный положительный `overlay_id` (или 0 для базы). Позднейшие overlays сцены не меняют ранее созданный task.
Состояния полигонов: `active` участвует в demand; `background_only` физически существует, но не требует дополнительного армирования; `removed` является физической пустотой.

### Компоненты и N
Расчётные компоненты создаются только при создании task, когда уже известны config, `back_grid/stock`, соответствие `load2cls` и фон class=0. Поэтому component_id относится к конкретному task analysis context, а не к scene. Background-only полигоны не образуют solver-компоненты.
В `PUT /v1/tasks` поле components принимает либо список явных индексов `[0,2,...]`, либо ровно один специальный селектор: `[-3]` — все реальные компоненты и их агрегированные комбинации; `[-1]` — прямой расчёт всего поля; `[-2]` — оба пути одновременно: реальные компоненты + их комбинации и прямой whole-field solve. Явные индексы валидируются после task-декомпозиции. При одной реальной компоненте whole является alias на component 0 и не создаёт повторных jobs.
N одного solver-запуска ограничен **100** и всегда трактуется как локальный N каждого выбранного расчётного объекта. Например `n=[1,2,3,4,5]` означает попытку этих N для каждой выбранной компоненты и/или whole, но не выше её собственного `max_useful_n`. Итоговые component-aggregate `total_N` рождаются из всех текущих feasible комбинаций локальных frontier и НЕ фильтруются исходным списком N. Новый запрос N дополняет существующие локальные расчёты; уже сохранённые пары component+N не пересчитываются. Локальный `N=infeasible` является нормальным результатом только этой пары: остальные N продолжают считаться. Для direct whole-field расчёта допустимый диапазон начинается с N=1 независимо от количества реальных компонент, а верхний useful bound равен сумме их `max_useful_n` (с учётом серверного hard limit при постановке jobs): одна whole-зона может покрывать несколько компонент через фон. Для одинакового итогового `total_N` whole и component aggregate конкурируют по фактической массе, а лучший результат может улучшаться по мере завершения workers. `max_useful_n=0` у нового анализа означает только отсутствие реальных компонент.

### Решения, масса и compact_zones
Координаты, диаметр, шаг и длина — **мм**; масса — **кг**. `proxy_mass` — целевая оценка оптимизатора, **не физические кг**. По умолчанию `anchor_factor=40`. После построения расчётной матрицы значение `-1` является жёстким барьером для solver-кандидатов, `0` означает проходимую область фонового армирования, положительные значения — классы дополнительного армирования. Для max-N прямоугольники дополнительно не могут проходить через `0` или клетки, не требующие соответствующий primitive recipe.
`actual_mass_kg` = `mass_metrics.with_anchorage_kg`: фактические стержни с анкеровкой, подрезанные по физическому контуру. Метрики: `without_anchorage_kg`, `with_anchorage_kg`, `without_anchorage_unclipped_kg`, `with_anchorage_unclipped_kg`.
`unclipped` продлевает те же выделенные дорожки дополнительной арматуры до полного прямоугольника; отверстия и граница не обрезают эти диагностические стержни. Фоновая арматура во всех четырёх суммах остаётся одинаковой и подрезанной; отдельные дополнительные массы находятся в `mass_metrics.additional`. Для метрик версии 2: `with_anchorage_unclipped_kg >= with_anchorage_kg` и `without_anchorage_unclipped_kg >= without_anchorage_kg`.
В решении `compact_zones` — упрощённое точное представление дополнительных зон: `origin`, единичный `direction` раскладки, полная anchored+unclipped `length`, `step`, количества `right`/`left` и диаметр `d`. Направление самого стержня получается поворотом `direction` на 90° против часовой стрелки.

### Проверка решений
Проверить сохранённое решение можно по `scene_id + task_id + N` (и, при необходимости, `component_id`), а произвольный список `compact_zones` — по `scene_id + smooth + overlay`. Необязательный `back_grid` добавляется как глобальный фон; если его нет/null, считается, что фон уже представлен среди zones.
Для каждого active-полигона возвращается процент обеспеченности по площади пересечений и величине армирования `As = 10*pi*(d/2)^2/step`. Для `background_only` результат всегда `100.0`, для `removed` — `null`; длина списка совпадает со стабильным списком source-polygons.

### Статусы и WebSocket
`completed` означает отсутствие дальнейших jobs, но не гарантирует наличие допустимого решения. Корректный solver-result `infeasible` для отдельного N не является `job_failed` и не ломает компоненту; `job_failed` используется для исключений/инфраструктурных ошибок. Причины глобально невозможной постановки смотрите в `analysis_infeasible`. `feasible` — допустимый вариант; `optimal`/`is_optimal` отражают флаги решённых подзадач, а не доказательство глобального минимума физической массы после layout.
`/v1/tasks/{task_id}/ws` отправляет сначала `snapshot`, затем события. Для новых task WebSocket использует сохранённый immutable-контекст; legacy параметры сохраняются для совместимости. Пауза и полная отмена относятся ко всей задаче; отмена не прерывает синхронный solver мгновенно.
"""

LEGACY_TAG = {
    "name": "99. Legacy v1",
    "description": INTRO,
}

V2_PATH_ORDER = [
    "/v2/dxf_upload",
    "/v2/json_upload",
    "/v2/tables_upload",
    "/v2/scenes/{scene_id}/polygons",
    "/v2/scenes/{scene_id}/overlays",
    "/v2/scenes/{scene_id}/overalys/{overlay_id}",
    "/v2/tasks",
    "/v2/tasks/{task_id}/n",
    "/v2/tasks/{task_id}/cancel",
    "/v2/tasks/{task_id}",
    "/v2/tasks/{task_id}/{n}",
    "/v2/bars",
    "/v2/bars/{bars_id}",
    "/v2/verification",
    "/v2/verification/{verification_id}",
    "/health/live",
    "/health/ready",
]


def _tag_for_path(path: str, legacy_group: int) -> str:
    if path.startswith("/v1/"):
        return LEGACY_TAG["name"]
    if path.startswith("/v2/dxf_upload") or path.startswith("/v2/json_upload") or path.startswith("/v2/tables_upload"):
        return V2_TAGS[0]["name"]
    if path.startswith("/v2/scenes/"):
        return V2_TAGS[1]["name"]
    if path.startswith("/v2/tasks"):
        return V2_TAGS[2]["name"]
    if path.startswith("/v2/bars") or path.startswith("/v2/verification"):
        return V2_TAGS[3]["name"]
    if path.startswith("/health/"):
        return SERVICE_TAG["name"]
    return LEGACY_OPERATION_GROUPS[legacy_group]["name"]


def _ordered_paths(paths: dict[str, Any], *, v2_only: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in V2_PATH_ORDER:
        if path in paths:
            out[path] = paths[path]
    if not v2_only:
        for path, value in paths.items():
            if path not in out and not path.startswith("/v1/"):
                out[path] = value
        for path, value in paths.items():
            if path.startswith("/v1/"):
                out[path] = value
    return out


def build_v2_openapi_schema(app: FastAPI) -> dict[str, Any]:
    cached = getattr(app.state, "v2_openapi_schema", None)
    if cached is not None:
        return cached
    schema = deepcopy(app.openapi())
    schema["info"]["title"] = "rebar-v2-api"
    schema["info"]["description"] = V2_INTRO
    schema["paths"] = _ordered_paths(schema.get("paths", {}), v2_only=True)
    schema["tags"] = deepcopy(V2_TAGS + [SERVICE_TAG])
    app.state.v2_openapi_schema = schema
    return schema


_UPLOAD = """\n\nMultipart: `file` — исходный файл, `config` — JSON-строка параметров (пример есть у поля). При `start=true` config с n обязателен.
При `start=false` config можно не передавать: API разбирает исходник и сохраняет готовые raw/smooth, solver не запускается.
Ошибки разбора содержимого возвращаются синхронно с кодом 422; успешный ответ означает, что исходные полигоны уже подготовлены.
Query-параметры `whole`, `scan_mode`, `component_result_top_k`, `validate_results` при наличии перекрывают config.
Размер исходника ограничен серверным `REBAR_MAX_UPLOAD_BYTES`; DWG не поддерживается."""
_CTX = "\n\nДля нового task используется сохранённый контекст сцены: smooth/overlay в запросе не нужны и не переопределяют его. Для мигрированных legacy-task без параметров используется raw/0. Чтение не запускает solver."
_METRICS = "\n\n`actual_mass_kg` включает анкеровку и подрезку. Остальные три массы — в `mass_metrics`; правила сравнения приведены в начале документации. Старые решения могут ещё не содержать актуальных метрик."

# name: (group index, summary, description)
OPERATIONS = {
    "start_v2_task": (3, "Создать задачу v2 для всего поля", "Создаёт whole-field задачу без component-декомпозиции. Все исходно запрошенные N переходят в preparing; PostgreSQL хранит их фактические состояния."),
    "add_v2_task_n": (3, "Добавить N в задачу v2", "Добавляет новые значения N. После готового preparing повторная подготовка не запускается; error/cancelled создают новый attempt, active/success не дублируются."),
    "cancel_v2_task_n": (3, "Отменить N задачи v2", "Помечает перечисленные N cancelled. Устаревшие jobs/attempt после завершения не имеют права продолжать downstream pipeline."),
    "get_v2_task": (3, "Получить состояния всех N задачи v2", "Возвращает все запрошенные N, включая незавершённые, с реальным persisted state из PostgreSQL."),
    "get_v2_task_n": (5, "Получить результат одного N задачи v2", "Возвращает текущее состояние N и, когда готово, whole-field zones, visible bar layout и mass metrics без component metadata."),
    "start_v2_bars": (5, "Запустить раскладку стержней v2", "Создаёт асинхронный bar-layout запрос по compact zones и возвращает bars_id."),
    "get_v2_bars": (5, "Получить раскладку стержней v2", "Возвращает persisted состояние запроса bars и готовый visible bar layout/mass metrics либо лаконичную ошибку."),
    "start_v2_verification": (5, "Запустить проверку армирования v2", "Создаёт асинхронную verification-операцию. Validation worker сам строит bar layout и проверяет исходные source polygons."),
    "get_v2_verification": (5, "Получить проверку армирования v2", "Возвращает persisted состояние verification и требуемое/фактическое армирование в см²/м и кг/м³."),
    "v2_create_scene_dxf_upload": (0, "Загрузить DXF-сцену v2", "Создаёт reusable scene из DXF и возвращает scene_id/state."),
    "v2_create_scene_json_upload": (0, "Загрузить JSON-сцену v2", "Создаёт reusable scene из лаконичного массива полигонов v2."),
    "v2_create_scene_tables_upload": (0, "Загрузить XLSX-сцену v2", "Создаёт reusable scene из таблиц nodes/elements/loads."),
    "v2_scene_polygons": (1, "Получить полигоны сцены v2", "Возвращает лаконичные source polygons выбранного raw/smooth overlay snapshot."),
    "v2_append_scene_overlays": (2, "Добавить overlays сцены v2", "Добавляет append-only clean/unclean операции и возвращает новый overlay_id snapshot."),
    "v2_get_scene_overlay": (2, "Получить overlay snapshot сцены v2", "Возвращает overlay events до выбранного snapshot по append-порядку, а не по числовому сравнению id."),
    "create_scene_dxf_upload": (0, "Создать сцену из DXF", "Загружает DXF, синхронно в API формирует raw/smooth и создаёт reusable scene_id без расчётных компонентов. Компоненты появятся только после создания task с известным фоном. Успешный ответ сразу возвращает state=ready."),
    "create_scene_json_upload": (0, "Создать сцену из JSON", "Загружает JSON source-polygons и создаёт scene_id без task_id. Raw и smooth подготавливаются синхронно в API; расчётные компоненты создаются позже на уровне task. Успешный ответ сразу ready."),
    "create_scene_tables_upload": (0, "Создать сцену из трёх XLSX", "Загружает nodes/elements/loads XLSX и синхронно в API формирует source-polygons и raw/smooth. Расчётные компоненты создаются позже на уровне task после определения фонового армирования. Успешный ответ сразу содержит ready scene_id."),
    "create_scene_pkl_upload": (0, "Создать сцену из pickle", "Загружает доверенный ограниченный pickle с полигонами и создаёт reusable scene_id без запуска анализа."),
    "get_scene": (1, "Получить состояние сцены", "Возвращает состояние scene_id. Для новых scene поле legacy components пусто: расчётные компоненты принадлежат task и зависят от его config/background. Новые upload-ручки возвращают scene уже ready; preparing сохраняется только для совместимости со старыми/восстанавливаемыми данными."),
    "scene_polygons": (1, "Получить полигоны сцены", "Возвращает полный стабильный список полигонов сцены для raw/smooth и выбранного overlay. overlay=-1 означает последний append-event, -2 — предпоследний; в ответе возвращается фактический положительный overlay_id."),
    "list_scene_overlays": (2, "Получить overlays сцены", "Возвращает append-only журнал overlay событий, принадлежащий scene_id."),
    "append_scene_overlays": (2, "Добавить overlays к сцене", "Добавляет события в append-only журнал сцены. Положительные id остаются клиентскими/непорядковыми; отрицательные значения используются только как селекторы при чтении/старте анализа."),
    'start_analysis': (3, 'Запустить анализ готовой сцены', 'Тело: scene_id, smooth, overlay_id, components, n и config. config — объект параметров либо JSON-строка как в upload. n — явный список снаружи config. API синхронно определяет фон и task-компоненты до постановки solver jobs. [-2] реальные компоненты+их aggregate и direct whole; [-3] реальные компоненты+их aggregate без direct whole; [-1] только direct whole; либо список неотрицательных task-component индексов. Отрицательный overlay сразу фиксируется как фактический положительный id. Scene ещё не готова — 409. Возвращает task_id и scene_id. Для безопасного повтора запроса клиенту следует избегать повторного создания task без контроля ответа.'),
    'verify_zones': (5, 'Проверить произвольные зоны армирования', 'Принимает scene_id, smooth, overlay_id и zones в компактном формате. direction — единичный вектор раскладки, стержень направлен на 90° против часовой стрелки. length — полная длина с анкеровкой без подрезки. При back_grid=[d,step] фон добавляется глобально; без back_grid фон должен быть среди zones. Проценты по среднему покрытию площади: 100*(фон+сумма As_i*area_intersection/area_polygon)/load. Это не проверка минимального локального покрытия и не строительная экспертиза. background_only=100, removed=null.'),
    'verify_task_solution': (5, 'Проверить сохранённое решение', 'scene_id должен совпадать со сценой task. По task_id+n берётся лучшее допустимое итоговое решение. Необязательный component_id выбирает одну компоненту; её реальные anchored+unclipped tracks формируются через layout. Фон берётся из сохранённого конфига и не дублируется среди дополнительных compact zones. Возвращаются средние по площади проценты в source-порядке и зафиксированный overlay_id; это не доказательство покрытия каждой точки.'),
    "live": (7, "Проверить, что API запущен", "Возвращает status=ok. Не проверяет PostgreSQL, Redis и наличие workers."),
    "ready": (7, "Проверить готовность API", "Проверяет подключение к PostgreSQL и Redis. 200 — готов, 503 — одно из хранилищ недоступно. Это не проверка завершения задач."),
    "create_task": (0, "Создать задачу из полигонов в JSON-теле", "Передайте параметры расчёта и input={kind: polygons, units: mm|m, polygons: [...]} в application/json. Обязательны n и input. Создаёт задачу и запускает подготовку/расчёт; start=false здесь нет. Для отложенного запуска используйте json_upload. Полигон задаётся points и load."),
    "create_task_upload": (0, "Загрузить DXF", "Только .dxf. Сохраняет привычный старому фронту контракт config + file; таблицы, JSON и pickle загружайте через отдельные ручки." + _UPLOAD),
    "create_task_tables_upload": (0, "Загрузить три таблицы XLSX", "Multipart: config, nodes_file, elements_file, loads_file, load_column. Требуются все три .xlsx: координаты узлов, элементы и нагрузки. Используется первый лист; координаты узлов исходного экспорта в метрах преобразуются в мм. load_column=1..4 выбирает столбец нагрузки, текущий default=1.\n\nAPI синхронно открывает таблицы и сохраняет готовые raw/smooth. При start=false solver не запускается; при start=true после импорта в worker отправляется уже вычислительная подготовка анализа. Ошибки строк/заголовков возвращаются с кодом 422. Query-параметры при наличии перекрывают config; каждый файл ограничен REBAR_MAX_UPLOAD_BYTES."),
    "create_task_json_upload": (0, "Загрузить файл исходных полигонов JSON", "Файл .json: список объектов {points: [[x,y],...], load: число, color: число} или объект {polygons: [...]}. Можно сохранить ответ source-polygons и загрузить его сюда. source_index, active, real, overlay_state старой задачи не переносятся: новая задача получает новый журнал overlays. Координаты ответа source-polygons уже в мм." + _UPLOAD),
    "create_task_pickle_upload": (0, "Загрузить pickle с полигонами NumPy/Shapely", "Файл .pickle/.pkl: список словарей с points, load, необязательными color и geometry. Поддерживаются числовые NumPy-массивы и Shapely Polygon в ограниченном загрузчике. Неизвестные globals/типы запрещены; поддержка произвольного Python pickle не обещается. Используйте только свои доверенные файлы. После нормализации хранятся canonical raw/smooth." + _UPLOAD),
    "source_polygons_upload": (0, "Разобрать DXF без создания задачи", "Принимает только file=.dxf и синхронно возвращает список исходных полигонов. Не создаёт task_id, не ставит jobs и не запускает solver. На большом файле HTTP-запрос может быть долгим; для обычной работы предпочтителен tasks/upload?start=false."),
    'get_task': (1, 'Получить состояние задачи', 'Snapshot: task, plan, n, status_counts, results. Контекст нового task неизменяем; ответ содержит scene_id и фактический overlay_id. Большие раскладки в snapshot не включаются.'),
    "get_source_polygons": (1, "Получить исходные полигоны raw/smooth", "Возвращает все полигоны со стабильным source_index, points, load, при наличии color, и состоянием overlay_state/active/real. Даже removed не удаляется из списка: индексы не сдвигаются. smooth=false — raw, true — сглаженные нагрузки. Для новых upload-задач raw/smooth готовы уже к моменту успешного ответа загрузки. GET не запускает подготовку." + _CTX),
    'list_overlays': (2, 'Получить журнал overlays сцены через task', 'Совместимый адрес: task_id разрешается в scene_id. Возвращает журнал событий сцены в порядке добавления. Сам task остаётся привязан к overlay_id, выбранному при создании.'),
    'append_overlays': (2, 'Добавить overlays к сцене через task', 'Тело — список событий {type,idxs,id,real}. Добавляет их в журнал scene соответствующей задачи. id положительный и уникален внутри сцены; порядок — append, а не величина id. clean/real=false создаёт пустоту, clean/real=true убирает дополнительную потребность, unclean возвращает полигон. Старый task не переключается на новые overlays: для другого состояния создайте новый task через PUT /v1/tasks.'),
    'list_components': (4, 'Получить компоненты анализа', 'Возвращает только реальные task-scoped компоненты, полученные после определения фонового армирования для зафиксированных variant+overlay+config. При одной компоненте список содержит только id=0; отдельной строки -1 нет. -1 не дублируется в списке реальных component rows; его чтение даёт сводную aggregate-информацию. Чтение не запускает solver.'),
    "get_component": (4, "Получить описание компонента", "Возвращает геометрические границы, polygon_indices, классы, состояния и max_useful_n. Для нового task чтение component_id=-1 возвращает виртуальный агрегат реальных компонент: aggregate max_useful_n является их суммой; при одной компоненте это alias на id=0 без повторного расчёта. Если реальных компонент нет, aggregate имеет max_useful_n=0. Legacy-task сохраняет старую whole-семантику." + _CTX),
    'schedule_component_n': (4, 'Запустить выбранные N одного компонента', 'Тело {n:[1,2,3]}. Нужна завершённая подготовка max-N реальных компонент, иначе 409. Для нового task schedule component_id=-1 означает direct whole-field solve для переданных локальных N; при одной реальной компоненте -1 маршрутизируется на component 0 и не дублирует расчёт. Component-aggregate totals возникают независимо при объединении frontier реальных компонент. Infeasible одного локального N не блокирует другие N. Ответ о постановке не означает завершение расчёта.'),
    "list_component_results": (4, "Получить сводку результатов компонента", "Для каждого N возвращает is_feasible, is_optimal, status/solve_state и proxy_mass. Это промежуточные solver/fit результаты, не масса готовой раскладки в кг. Пустой список означает отсутствие сохранённых результатов для этого контекста." + _CTX),
    "get_component_result": (4, "Получить полный результат компонента для N", "Возвращает сохранённый frontier: solver/fit, прямоугольники, anchored_boxes и диагностические поля. Геометрии сериализуются в JSON. Это ещё не глобальная раскладка всех компонентов. Если результата нет — 404." + _CTX),
    'list_solutions': (5, 'Получить список финальных решений', 'Лёгкая сводка без bars: solution_id, source, total_N, component_ns, proxy_mass, actual_mass_kg, статусы и URL. Новый task всегда использует свой неизменяемый smooth/overlay; legacy-task по умолчанию raw/0. Фильтры total_n, source, status. На одно N может быть несколько вариантов.'),
    'get_solution': (5, 'Получить конкретную раскладку по solution_id', 'Полный JSON: bar_layout, compact_zones дополнительных стержней, mass_metrics. Для нового task контекст фиксирован. Исторический solution_id без overlay возвращает своё исходное состояние raw/smooth/overlay, не скрывая другие старые анализы. При явно неверном overlay — 404.'),
    "component_events": (6, "Получить события с постраничным смещением", "Ответ: {task_id, overlay_id, events}. start — число записей, которые нужно пропустить (SQL OFFSET), НЕ id события. Выдача по возрастанию id, текущий предел — 10000 записей. Здесь сохраняются не только компонентные, но и другие события выбранного overlay; отдельного фильтра smooth нет."),
    'list_results': (5, 'Получить результаты в формате старого фронта', 'Словарь сводок с N в качестве ключа. Лучшее решение выбирается по допустимости, затем минимальной actual_mass_kg; is_optimal используется только при равной массе. Для новых task решения могут приходить из двух источников: `components` (комбинации реальных component frontier) и `whole` (direct whole-field solve). Для одного total_N лучшим считается минимальный actual_mass_kg, и результат может улучшаться по мере расчёта. Для новых task контекст зафиксирован, для legacy без параметров raw/0. Это не рейтинг по массе unclipped.'),
    'get_result': (5, 'Получить лучший результат для N (старый формат)', 'Совместимые solver_result, fit_result и summary. Выбор: допустимость, минимальная actual_mass_kg с анкеровкой и подрезкой; is_optimal — при равной массе. Новые task используют сохранённый контекст, legacy без параметров raw/0. Результат может улучшаться во время расчёта. Нет решения — 404.'),
    'get_result_dxf': (5, 'Скачать DXF лучшего результата для N', 'Выбирает то же решение, что results/{n}, и наносит зоны на исходный DXF сцены. Нет исходного DXF — 409; нет решения — 404. Новый task использует свой контекст; legacy без параметров raw/0. Ответ — application/dxf.'),
    "get_events": (6, "Читать новые события после курсора", "Возвращает массив событий с id строго больше after. Для первого запроса after=0-0; далее передавайте id последнего полученного события. count сервер ограничивает диапазоном 1..1000. Пустой массив не означает завершения задачи. Фильтрация по overlay; raw/smooth различаются полем variant в событиях. Основные события: source_materialized, components_ready, component_n_finished, solution_available, n_finished, analysis_infeasible, job_failed, task_state."),
    "add_n": (3, "Запустить или дополнить расчёт N", "Тело: {\"n\":5} или {\"n\":[1,2,5]}. Для нового scene-task компоненты уже определены при создании task; запрос дополняет локальный список requested N и планирует ещё не рассчитанные пары component/whole+N с отсечением по их max_useful_n. Итоговые aggregate total_N не добавляются обратно в requested N и не ограничиваются им. Legacy-task может по-прежнему лениво поставить старую подготовку. GET /components сам solver не запускает. Возвращается план, а не готовый результат." + _CTX),
    "cancel": (3, "Отменить задачу или отдельные N", "Тело {} (также n=null/[]) отменяет ВСЮ задачу, независимо от smooth/overlay. Тело {\"n\":[2,3]} отменяет N выбранного raw/smooth и overlay. Полная отмена сохраняет исходники/результаты, помечает задачу и делает старые jobs неактуальными. Выполняющийся синхронный solver может завершить текущий вызов; это не мгновенное убийство pod."),
    "pause_task": (3, "Приостановить обработку задачи", "Без тела запроса. Пауза относится ко всей задаче: новые jobs временно откладываются. Уже выполняющийся solver не прерывается мгновенно. Данные не удаляются. Ответ — текущий план; для продолжения используйте resume."),
    "resume_task": (3, "Снять паузу с задачи", "Без тела запроса. Снимает paused и разрешает обработку ожидающих jobs. Не восстанавливает полностью отменённую задачу, не создаёт заново упавшие jobs и не пересчитывает сохранённые метрики автоматически. Ответ — план."),
}

PARAMETERS = {
    "scene_id": "Идентификатор переиспользуемой сцены, полученный из /v1/scenes/*_upload или старой upload-ручки.",
    "task_id": "Идентификатор задачи из ответа загрузки (32 шестнадцатеричных символа).",
    "bars_id": "Идентификатор асинхронной операции раскладки стержней из POST /v2/bars.",
    "verification_id": "Идентификатор асинхронной операции проверки армирования из POST /v2/verification.",
    "overlay_id": "Идентификатор/селектор snapshot overlay сцены v2: 0 — исходное состояние, положительный id — точный snapshot, отрицательные значения — snapshot с конца append-порядка.",
    "component_id": "ID реальной task-компоненты. Чтение -1 даёт aggregate-сводку; запуск N для -1 у нового task означает direct whole-field solve, а при K=1 является alias на component 0. Legacy-task сохраняет исторический whole.",
    "solution_id": "Идентификатор конкретного решения из списка solutions.",
    "n": "Число зон N, для которого читается сохранённый результат.",
    "overlay": "Селектор сцены: 0 — база, положительный id — точное событие, -1/-2 — событие с конца по append-порядку. У нового task игнорируется: используется сохранённый overlay_id.",
    "smooth": "false — raw; true — сглаженные нагрузки smooth. Если не передан, используется raw.",
    "start": "true — импорт, подготовка и автоматический расчёт; false — только импорт raw/smooth, затем запускайте /n вручную.",
    "scan_mode": "Сохраняется для совместимости. Новый scene-task всегда планирует только запрошенные N, ограниченные max-N MILP и потолком 100; hard не добавляет незапрошенные значения.",
    "whole": "Перекрывает config.whole для совместимых upload-маршрутов. В новом task whole-path означает direct whole-field solve; component aggregate строится отдельно из реальных компонент.",
    "component_result_top_k": "Перекрывает config: число лучших по proxy комбинаций компонентов на суммарное N, 1..100.",
    "validate_results": "Перекрывает config: запустить дополнительную проверку готовых допустимых решений.",
    "total_n": "Фильтр по суммарному total_N финального решения; без параметра — все N.",
    "source": "Фильтр источника: components (объединённые компоненты) или whole (всё поле).",
    "status": "Фильтр строкового статуса решения, например optimal, feasible, infeasible; без параметра — все.",
    "after": "Курсор id последнего события; в первый раз 0-0. Возвращаются только более новые записи.",
    "count": "Размер порции событий; сервер ограничивает значение диапазоном 1..1000.",
}

MODEL_FIELDS = {
    "n": "N как число, список или диапазон {start, stop, coarse_step}. Это число зон подзадачи, не бюджет <=N.",
    "back_grid": "Фоновая сетка [диаметр мм, шаг мм]. Не задавайте back_grid и stock для автоподбора из каталога.",
    "stock": "Разрешённые дополнительные наборы [[диаметр мм, шаг мм], ...]. Передача включает пользовательский набор.",
    "max_layers": "Лимит дополнительных слоёв при построении recipes. Эффективный набор фиксируется при подготовке анализа.",
    "min_width_mm": "Минимальная поперечная ширина зоны на стадии fit, мм.",
    "steel_density_kg_m3": "Плотность стали для физических масс, кг/м³; по умолчанию 7850.",
    "anchor_factor": "Запрошенная длина анкеровки с каждого конца = factor × диаметр, мм. Контур может её подрезать.",
    "axis": "Мировое направление стержней: x — горизонтально, y — вертикально.",
    "max_snap_mm": "Сохраняемый параметр совместимости старого клиента; в текущем pipeline не передаётся напрямую в layout_rebars.",
    "min_bar_gap_mm": "Сохраняемый параметр совместимости; текущий layout использует свои правила разведения стержней, не этот аргумент напрямую.",
    "scan_mode": PARAMETERS["scan_mode"].replace("Перекрывает config.scan_mode: ", ""),
    "whole": "Запрашивать direct whole-field solve в дополнение к компонентным комбинациям там, где маршрут поддерживает этот флаг; при одной реальной компоненте повторный whole solve не создаётся.",
    "component_result_top_k": "Сколько лучших proxy-комбинаций компонентов сохранять на суммарное N; не гарантия лучшего физического кг.",
    "validate_results": "Дополнительная проверка готового допустимого решения.",
    "max_concurrent_jobs": "Лимит одновременно занятых jobs задачи; ограничивается сервером, не число потоков solver.",
    "quantizer": "Сохраняемый блок совместимости. Текущий компонентный pipeline использует серверные настройки подготовки и не передаёт этот блок напрямую.",
    "solver": "Настройки solver и ограничений его выполнения; серверные лимиты имеют приоритет.",
    "input": "Исходные полигоны для JSON-тела create_task: kind, units, polygons.",
    "kind": "Для прямого JSON-входа — polygons.", "units": "Единицы входных координат: mm или m; внутри расчёта координаты в мм.",
    "polygons": "Непустой список полигонов с points и load.",
    "points": "Вершины [[x,y], ...], минимум 3; единицы определяются units, при source-polygons — мм.",
    "load": "Требуемая интенсивность армирования, которую сопоставляют с фоном и recipes; не масса в кг.",
    "start": "Начало включительного диапазона N.", "stop": "Конец включительного диапазона N, не меньше start.",
    "coarse_step": "Шаг первого грубого прохода. Затем диапазон уточняется промежуточными N; это не прореживание итогового диапазона.",
    "method": "Метод квантования: exact или heuristic.",
    "preserve_holes": "Сохранять отверстия при квантовании геометрии.",
    "max_shift_fraction": "Допустимая доля сдвига координат квантователем.",
    "shrink_penalty": "Вес штрафа за уменьшение полигона при квантовании.",
    "expand_penalty": "Вес штрафа за увеличение полигона при квантовании.",
    "load_gamma": "Параметр весов нагрузки в квантователе.",
    "min_shrink_tol_ratio": "Минимальный относительный допуск на уменьшение.",
    "min_expand_tol_ratio": "Минимальный относительный допуск на увеличение.",
    "coord_eps": "Численный допуск сравнения координат, мм.",
    "target_cells_x": "Желаемое число ячеек по X; null — автоматическое.",
    "target_cells_y": "Желаемое число ячеек по Y; null — автоматическое.",
    "backend": "Запрошенный движок оптимизации highs или cbc.",
    "timeout_seconds": "Таймаут внешнего вызова solver, секунды; null — использовать серверную настройку.",
    "solver_time_limit": "Лимит времени оптимизатора, секунды; null — серверная настройка.",
    "threads": "Число потоков solver; проверяется относительно серверного максимума.",
    "require_optimal": "Требовать оптимальность в solver-подзадаче; это не доказательство минимума итоговой физической массы.",
    "return_best_on_timeout": "Параметр совместимости: текущий pipeline вызывает solver с return_best_on_timeout=True.",
    "use_warm_start": "Сохраняемый параметр solver; текущий компонентный pipeline не передаёт его напрямую в backend.",
    "cross_n_warm_start": "Сохраняемый параметр solver; текущий компонентный pipeline не передаёт его напрямую в backend.",
    "emit_interval": "Сохраняемый параметр solver; текущий компонентный pipeline не передаёт его напрямую в backend.",
    "emit_every_nodes": "Сохраняемый параметр solver; текущий компонентный pipeline не передаёт его напрямую в backend.",
    "emit_heartbeat": "Сохраняемый параметр solver; текущий компонентный pipeline не передаёт его напрямую в backend.",
    "highs_options": "Сохраняемый параметр solver; текущий компонентный pipeline не передаёт его напрямую в backend.",
    "prepared_max_n": "Ограничение/подсказка подготовки матрицы кандидатов; проверяется серверный максимум N.",
    "build_pulp_template": "Сохраняемый параметр solver; текущий компонентный pipeline не передаёт его напрямую в backend.",
    "postprocess_intermediate": "Сохраняемый параметр solver; текущий компонентный pipeline не передаёт его напрямую в backend.",
    "type": "clean — исключить, unclean — вернуть полигон.",
    "idxs": "Список source_index из исходных полигонов; индексы стабильны, повторения удаляются.",
    "id": "Положительный клиентский ID события; уникален внутри сцены. Порядок задаётся append, а не числом id.",
    "real": "При clean: false — вырезать физически; true — оставить поле, убрать дополнительный demand. При unclean игнорируется.",
    "task_id": PARAMETERS["task_id"], "state": "Текущее состояние обработки, не доказательство оптимальности.",
    "websocket_url": "Относительный URL WebSocket событий задачи.",
    "status_url": "Относительный URL snapshot задачи.",
}

BODY_FIELDS = {
    "config": "Поле config — строка с JSON-объектом TaskParameters, не файл. При start=true обязательны config и n; при start=false можно оставить пустым. Пример ниже использует автокаталог (без back_grid/stock).",
    "file": "Исходный файл формата, указанного в описании ручки; передаётся как binary multipart.",
    "nodes_file": "XLSX узлов: номер узла и X/Y/Z; координаты исходного табличного экспорта в метрах.",
    "elements_file": "XLSX элементов со ссылками на номера узлов.",
    "loads_file": "XLSX значений нагрузки/армирования для элементов.",
    "load_column": "Номер столбца нагрузки 1..4; текущий default=1. Рекомендуется задавать явно.",
}


def _references(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (v.replace("#/$defs/", "#/components/schemas/") if k == "$ref" else _references(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_references(x) for x in value]
    return value


def install_russian_docs(app: FastAPI) -> None:
    """Decorate the generated schema only; keep public paths, models and defaults."""
    from .models import TaskParameters
    original_openapi = app.openapi
    app.openapi_schema = None
    decorated = False

    def openapi() -> dict[str, Any]:
        nonlocal decorated
        schema = original_openapi()
        if decorated:
            return schema
        schema["info"].update(title="rebar-v2-api", description=V2_INTRO)
        schema["tags"] = deepcopy(V2_TAGS + [SERVICE_TAG, LEGACY_TAG])
        schemas = schema.setdefault("components", {}).setdefault("schemas", {})
        config_schema = _references(TaskParameters.model_json_schema())
        definitions = config_schema.pop("$defs", {})
        for name, definition in definitions.items():
            schemas.setdefault(name, definition)
        schemas["TaskParameters"] = config_schema
        config_json = json.dumps(CONFIG_EXAMPLE, ensure_ascii=False, indent=2)
        for name, model in schemas.items():
            fields = model.get("properties", {})
            for key, field in fields.items():
                desc = BODY_FIELDS.get(key) if name.startswith("Body_") else MODEL_FIELDS.get(key)
                if desc:
                    field["description"] = desc
            if name.startswith("Body_") and "config" in fields:
                fields["config"]["examples"] = [config_json]
            if name == "ComponentNRequest":
                fields["n"]["description"] = "Непустой список положительных N компонента, например [1,2,3]."
                model["examples"] = [{"n": [1, 2, 3]}]
            elif name == "NMutation":
                fields["n"]["description"] = "Положительное N либо список положительных N для планирования компонентов."
                model["examples"] = [{"n": [1, 2, 3]}]
            elif name == "CancelMutation":
                fields["n"]["description"] = "Список отменяемых N. Отсутствие, null или [] означает полную отмену задачи."
                model["examples"] = [{}, {"n": [2, 3]}]
            elif name == "OverlayEventMutation":
                model["examples"] = [{"type": "clean", "idxs": [3, 7], "id": 67689, "real": True}]
            elif name == "AnalysisTaskStart":
                fields["config"]["description"] = "Объект параметров расчёта или JSON-строка как в upload; n, scene_id и selectors задаются снаружи. Конфликтующие дубликаты запрещены."
                model["examples"] = [{"scene_id": "scene-id", "overlay_id": -1, "smooth": True,
                                      "components": [-2], "n": [1, 2, 5],
                                      "config": {"max_layers": 2, "anchor_factor": 40, "axis": "x"}}]
            elif name == "TaskParameters":
                model["examples"] = [deepcopy(CONFIG_EXAMPLE)]
            elif name == "TaskCreate":
                model["examples"] = [{**deepcopy(CONFIG_EXAMPLE), "input": {"kind": "polygons", "units": "mm", "polygons": [{"points": [[0, 0], [2000, 0], [2000, 2000], [0, 2000]], "load": 19}]}}]

        for route in app.routes:
            if not isinstance(route, APIRoute) or not route.include_in_schema:
                continue
            spec = OPERATIONS.get(route.name)
            if spec is None:
                continue
            group, summary, description = spec
            for method in route.methods:
                op = schema["paths"][route.path_format][method.lower()]
                op.update(summary=summary, description=description, tags=[_tag_for_path(route.path_format, group)])
                for p in op.get("parameters", []):
                    name = p["name"]
                    desc = PARAMETERS.get(name)
                    if name == "smooth" and "{task_id}" in route.path_format:
                        desc = "Новый task: сохранённый контекст, query игнорируется. Legacy-task: false — raw (default), true — smooth."
                    elif name == "start" and route.name == "component_events":
                        desc = "Количество пропускаемых событий (OFFSET), не id. По умолчанию 0."
                    if desc:
                        p["description"] = desc
                for code, response in op.get("responses", {}).items():
                    if code.startswith("2"):
                        response["description"] = "Запрос принят в обработку" if code == "202" else "Успешный ответ; состав данных описан выше"
                    elif code == "422":
                        response["description"] = "Некорректные параметры, JSON config или тело запроса"
                if "{task_id}" in route.path_format:
                    op["responses"].setdefault("404", {"description": "Задача, контекст или запрошенные данные не найдены/ещё не готовы"})
                if route.name in {"create_task_upload", "create_task_tables_upload", "create_task_json_upload", "create_task_pickle_upload", "source_polygons_upload"}:
                    op["responses"].update({"413": {"description": "Превышен лимит размера исходного файла"}, "415": {"description": "Расширение/формат файла не поддерживается этой ручкой"}})
                if route.name == "schedule_component_n":
                    op["responses"]["409"] = {"description": "Анализ не подготовлен; сначала вызовите POST /tasks/{task_id}/n"}
                if route.name == "get_result_dxf":
                    op["responses"]["409"] = {"description": "Нет исходного DXF или экспорт этого результата невозможен"}
                    op["responses"]["200"]["content"] = {"application/dxf": {"schema": {"type": "string", "format": "binary"}}}
                if route.name == "ready":
                    op["responses"]["503"] = {"description": "PostgreSQL или Redis недоступен"}
                if route.name == "append_overlays":
                    op["requestBody"]["content"]["application/json"]["example"] = [{"type": "clean", "idxs": [3, 7], "id": 67689, "real": True}]
        schema["paths"] = _ordered_paths(schema.get("paths", {}), v2_only=False)
        app.state.v2_openapi_schema = None
        decorated = True
        return schema

    app.openapi = openapi
