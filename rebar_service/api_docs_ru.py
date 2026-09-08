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

TAGS = [
    {"name": "1. Загрузка", "description": "Создание задач из DXF, таблиц XLSX, JSON или pickle. Форматы разделены по ручкам."},
    {"name": "2. Задача и полигоны", "description": "Состояние задачи и исходные полигоны raw/smooth."},
    {"name": "3. Оверлеи", "description": "Неизменяемый журнал исключения/возврата полигонов. Ревизия выбирается через overlay."},
    {"name": "4. Запуск и управление", "description": "Первый запуск сам подготавливает анализ; пауза, продолжение и отмена."},
    {"name": "5. Компоненты", "description": "Декомпозиция и промежуточные результаты отдельных компонентов. -1 обозначает whole."},
    {"name": "6. Решения и экспорт", "description": "Финальные раскладки, совместимый со старым фронтом формат results и DXF."},
    {"name": "7. События", "description": "Прогресс и причины ошибок. events — курсор по id, component-events — смещение OFFSET."},
    {"name": "8. Состояние сервиса", "description": "Проверки процесса API и доступности хранилищ."},
]

INTRO = """## Как начать
1. Загрузите файл через нужную ручку из группы **Загрузка**. В multipart поле `config` — **строка JSON**, не имя файла.
2. Ответ с `task_id` означает приём задачи, а не завершение расчёта. Ожидайте события `source_materialized`, затем читайте `source-polygons`.
3. При `start=true` расчёт продолжается автоматически. При `start=false` получите полигоны, при необходимости добавьте overlays, затем вызовите `POST /v1/tasks/{task_id}/n`.
4. Читайте `/solutions`, затем решение по `solution_id`; `/results/{n}` — старый формат представления лучшего решения для данного N.

### Контекст анализа
Контекст — **task_id + raw/smooth + overlay**. `overlay=0` (или отсутствие параметра) — исходные полигоны, **не последняя ревизия**.
Ненулевой overlay применяет журнал по порядку добавления до указанного события включительно. Всегда передавайте тот же контекст при запуске и чтении.
У большинства ручек `smooth=false` означает raw. У `/solutions` отсутствие smooth выбирает **оба** варианта; у `/results*` — `initial_variant` задачи.

### N и статусы
В этой версии N — число прямоугольников в решаемой модели, **не бюджет «не более N»**. Составные рецепты могут давать несколько физических слоёв.
`/n` планирует N для компонентов; итоговый `total_N` — сумма выбранных компонентных N и может отличаться от запрошенного N.
`feasible` — допустимый вариант; `optimal`/`is_optimal` отражают флаги решённых подзадач, а не доказательство глобального минимума физической массы после layout.
`completed` означает отсутствие дальнейших jobs, но не гарантию наличия допустимого решения. Причины смотрите в событиях (`analysis_infeasible`, `job_failed`).

### Масса и геометрия
Координаты, диаметр, шаг и длина — **мм**; масса — **кг**. `proxy_mass` — целевая оценка оптимизатора, **не физические кг**.
`actual_mass_kg` = `mass_metrics.with_anchorage_kg`: фактические стержни с анкеровкой, подрезанные по физическому контуру.
Метрики: `without_anchorage_kg`, `with_anchorage_kg`, `without_anchorage_unclipped_kg`, `with_anchorage_unclipped_kg`.
`unclipped` продлевает те же выделенные дорожки дополнительной арматуры до полного прямоугольника; отверстия и граница не обрезают эти диагностические стержни.
Это **не новый расчёт сетки**: поперечные позиции сохраняются. Фоновая арматура во всех четырёх суммах остаётся одинаковой и подрезанной;
отдельные дополнительные массы находятся в `mass_metrics.additional`. Поля `final rectangle*` — ограничивающие прямоугольники, а не точная форма отверстий; фактические отрезки находятся в `bars*`.
Для метрик версии 2: `with_anchorage_unclipped_kg >= with_anchorage_kg` и `without_anchorage_unclipped_kg >= without_anchorage_kg`.
Обновление кода **не меняет старые JSON в БД**: им нужен повторный layout; проверяйте `mass_metrics.schema_version` / `summary.mass_metrics_version`.

### WebSocket
`/v1/tasks/{task_id}/ws?overlay=0&after=0-0` — WebSocket, поэтому отдельной HTTP-строки в Swagger нет.
Первое сообщение — `snapshot`, затем события. Команды: `{"action":"snapshot"}`, `{"action":"add","n":[1,2],"smooth":false,"overlay":0}`,
`{"action":"cancel","n":[2]}`, `{"action":"pause_range"}`, `{"action":"resume_range"}`, `{"action":"cancel_task"}`.
Пауза и полная отмена относятся ко всей задаче; `cancel` с N — к выбранному контексту. Отмена не прерывает синхронный solver мгновенно.
"""

_UPLOAD = """\n\nMultipart: `file` — исходный файл, `config` — JSON-строка параметров (пример есть у поля). При `start=true` config с n обязателен.
При `start=false` config можно не передавать: сервер сохраняет исходник, worker `materialize_source` строит raw/smooth, solver не запускается.
После приёма ошибки разбора содержимого приходят через `job_failed`; код 200 не означает, что файл успешно разобран.
Query-параметры `whole`, `scan_mode`, `component_result_top_k`, `validate_results` при наличии перекрывают config.
Размер исходника ограничен серверным `REBAR_MAX_UPLOAD_BYTES`; DWG не поддерживается."""
_CTX = "\n\nИспользуйте те же smooth и overlay, с которыми запускали расчёт. Чтение само не запускает solver."
_METRICS = "\n\n`actual_mass_kg` включает анкеровку и подрезку. Остальные три массы — в `mass_metrics`; правила сравнения приведены в начале документации. Старые решения могут ещё не содержать актуальных метрик."

# name: (group index, summary, description)
OPERATIONS = {
    "live": (7, "Проверить, что API запущен", "Возвращает status=ok. Не проверяет PostgreSQL, Redis и наличие workers."),
    "ready": (7, "Проверить готовность API", "Проверяет подключение к PostgreSQL и Redis. 200 — готов, 503 — одно из хранилищ недоступно. Это не проверка завершения задач."),
    "create_task": (0, "Создать задачу из полигонов в JSON-теле", "Передайте параметры расчёта и input={kind: polygons, units: mm|m, polygons: [...]} в application/json. Обязательны n и input. Создаёт задачу и запускает подготовку/расчёт; start=false здесь нет. Для отложенного запуска используйте json_upload. Полигон задаётся points и load."),
    "create_task_upload": (0, "Загрузить DXF", "Только .dxf. Сохраняет привычный старому фронту контракт config + file; таблицы, JSON и pickle загружайте через отдельные ручки." + _UPLOAD),
    "create_task_tables_upload": (0, "Загрузить три таблицы XLSX", "Multipart: config, nodes_file, elements_file, loads_file, load_column. Требуются все три .xlsx: координаты узлов, элементы и нагрузки. Используется первый лист; координаты узлов исходного экспорта в метрах преобразуются в мм. load_column=1..4 выбирает столбец нагрузки, текущий default=1.\n\nAPI сохраняет комплект, worker materialize_source открывает таблицы. start=false строит только raw/smooth, start=true продолжает расчёт. Ошибки строк/заголовков появятся в событиях. Query-параметры при наличии перекрывают config; каждый файл ограничен REBAR_MAX_UPLOAD_BYTES."),
    "create_task_json_upload": (0, "Загрузить файл исходных полигонов JSON", "Файл .json: список объектов {points: [[x,y],...], load: число, color: число} или объект {polygons: [...]}. Можно сохранить ответ source-polygons и загрузить его сюда. source_index, active, real, overlay_state старой задачи не переносятся: новая задача получает новый журнал overlays. Координаты ответа source-polygons уже в мм." + _UPLOAD),
    "create_task_pickle_upload": (0, "Загрузить pickle с полигонами NumPy/Shapely", "Файл .pickle/.pkl: список словарей с points, load, необязательными color и geometry. Поддерживаются числовые NumPy-массивы и Shapely Polygon в ограниченном загрузчике. Неизвестные globals/типы запрещены; поддержка произвольного Python pickle не обещается. Используйте только свои доверенные файлы. После нормализации хранятся canonical raw/smooth." + _UPLOAD),
    "source_polygons_upload": (0, "Разобрать DXF без создания задачи", "Принимает только file=.dxf и синхронно возвращает список исходных полигонов. Не создаёт task_id, не ставит jobs и не запускает solver. На большом файле HTTP-запрос может быть долгим; для обычной работы предпочтителен tasks/upload?start=false."),
    "get_task": (1, "Получить состояние задачи", "Возвращает snapshot: task, plan, n, status_counts, results. Это агрегированное состояние задачи; initial_variant определяет основной вариант snapshot. Для конкретной пары smooth/overlay используйте компоненты, решения и события с нужными query-параметрами. Большие раскладки в snapshot не включаются."),
    "get_source_polygons": (1, "Получить исходные полигоны raw/smooth", "Возвращает все полигоны со стабильным source_index, points, load, при наличии color, и состоянием overlay_state/active/real. Даже removed не удаляется из списка: индексы не сдвигаются. smooth=false — raw, true — сглаженные нагрузки. До source_materialized возможен 404; после завершения импорта повторите запрос. GET не запускает подготовку." + _CTX),
    "list_overlays": (2, "Получить весь журнал overlays", "Ответ: {task_id, overlays: [...]}. Журнал общий для raw и smooth, порядок определяется добавлением (seq), а не величиной клиентского id. По умолчанию он пуст. Получение журнала не меняет запущенные расчёты."),
    "append_overlays": (2, "Добавить изменения полигонов", "Тело — список событий, например [{\"type\":\"clean\",\"idxs\":[3,7],\"id\":67689,\"real\":true}]. Список атомарно добавляется в конец журнала; ответ содержит весь журнал. id положительный и уникален внутри задачи; повтор id даёт 422. idxs — стабильные индексы source-polygons.\n\nclean + real=false: физически исключить полигон; clean + real=true: оставить физический контур, убрать потребность в дополнительном армировании. unclean возвращает активное состояние; для уже активного полигона ничего не меняет, real при возврате игнорируется. Дождитесь raw/smooth до отправки idxs. Solver сам не запускается: вызовите /n с overlay=id нужного события."),
    "list_components": (4, "Получить компоненты анализа", "Возвращает список компонентов и индексы active/background_only/degenerate. До подготовки список может быть пуст. Подготовка выполняется первым POST /n; удалённую ручку components/prepare вызывать не надо. component_id действителен только в данном контексте и может измениться при другом overlay." + _CTX),
    "get_component": (4, "Получить описание компонента", "Возвращает геометрические границы, polygon_indices, классы, состояния и max_useful_n. component_id=-1 означает весь контур (whole). Для whole может возвращаться описание available без рассчитанного max_useful_n. Нет компонента/поля — 404." + _CTX),
    "schedule_component_n": (4, "Запустить выбранные N одного компонента", "Тело: {\"n\":[1,2,3]}. N положительные; дубликаты удаляются. Анализ должен быть prepared, иначе 409: сначала запустите /tasks/{task_id}/n. Для обычной компоненты N не должен превышать max_useful_n. component_id=-1 запускает whole; его подготовка при необходимости ставится в очередь. Ответ 202 сообщает о постановке, а не о готовом решении." + _CTX),
    "list_component_results": (4, "Получить сводку результатов компонента", "Для каждого N возвращает is_feasible, is_optimal, status/solve_state и proxy_mass. Это промежуточные solver/fit результаты, не масса готовой раскладки в кг. Пустой список означает отсутствие сохранённых результатов для этого контекста." + _CTX),
    "get_component_result": (4, "Получить полный результат компонента для N", "Возвращает сохранённый frontier: solver/fit, прямоугольники, anchored_boxes и диагностические поля. Геометрии сериализуются в JSON. Это ещё не глобальная раскладка всех компонентов. Если результата нет — 404." + _CTX),
    "list_solutions": (5, "Получить список финальных решений", "Лёгкая сводка без полных bars и mass_metrics: solution_id, source, total_N, component_ns, proxy_mass, actual_mass_kg, статусы и result_url. Можно фильтровать total_n, source=components|whole и status. Без smooth возвращаются оба варианта; overlay по умолчанию 0. На одно N может быть несколько решений. Это список найденных вариантов, не гарантированная монотонная кривая массы."),
    "get_solution": (5, "Получить финальную раскладку по solution_id", "Возвращает полный сохранённый JSON решения, включая bar_layout, bars, зоны и mass_metrics. Передавайте overlay из сводки; при неверном контексте/ID — 404. solution_id уже определяет вариант raw/smooth, поэтому отдельного smooth здесь нет." + _METRICS),
    "component_events": (6, "Получить события с постраничным смещением", "Ответ: {task_id, overlay_id, events}. start — число записей, которые нужно пропустить (SQL OFFSET), НЕ id события. Выдача по возрастанию id, текущий предел — 10000 записей. Здесь сохраняются не только компонентные, но и другие события выбранного overlay; отдельного фильтра smooth нет."),
    "list_results": (5, "Получить результаты в формате старого фронта", "Словарь сводок с N в качестве ключа. Для каждого N выбирается лучшее решение текущим ранжированием: допустимость, is_optimal, затем actual_mass_kg и proxy_mass. Без smooth используется initial_variant задачи; overlay=0 по умолчанию. Полных стержней в списке нет. Для минимальной массы unclipped надо отдельно сравнивать mass_metrics решений, не этот рейтинг."),
    "get_result": (5, "Получить лучший результат для N (старый формат)", "Возвращает совместимые solver_result, fit_result и summary. Выбор идёт по допустимости, is_optimal, затем фактической clipped-массе; это не выбор минимума unclipped. Без smooth берётся initial_variant. summary содержит четыре массы, zones и bars с/без анкеровки и подрезки. Если результата нет — 404." + _METRICS),
    "get_result_dxf": (5, "Скачать DXF лучшего результата для N", "Выбирает то же решение, что results/{n}, и накладывает зоны на исходный DXF. Для задач из JSON/XLSX/pickle без исходного DXF экспорт возвращает 409; отсутствующий результат — 404. Ответ — файл application/dxf. Без smooth используется initial_variant; обязательно укажите нужный overlay."),
    "get_events": (6, "Читать новые события после курсора", "Возвращает массив событий с id строго больше after. Для первого запроса after=0-0; далее передавайте id последнего полученного события. count сервер ограничивает диапазоном 1..1000. Пустой массив не означает завершения задачи. Фильтрация по overlay; raw/smooth различаются полем variant в событиях. Основные события: source_materialized, components_ready, component_n_finished, solution_available, n_finished, analysis_infeasible, job_failed, task_state."),
    "add_n": (3, "Запустить или дополнить расчёт N", "Тело: {\"n\":5} или {\"n\":[1,2,5]}. Если анализ ещё не подготовлен, первый запрос ставит подготовку в очередь и сохраняет N; последующие запросы пополняют список. После подготовки запускаются компоненты и, если включён, whole. N здесь назначаются отдельным компонентам; total_N объединённого решения может быть другим. GET /components сам подготовку не запускает. Возвращается план, а не готовый результат." + _CTX),
    "cancel": (3, "Отменить задачу или отдельные N", "Тело {} (также n=null/[]) отменяет ВСЮ задачу, независимо от smooth/overlay. Тело {\"n\":[2,3]} отменяет N выбранного raw/smooth и overlay. Полная отмена сохраняет исходники/результаты, помечает задачу и делает старые jobs неактуальными. Выполняющийся синхронный solver может завершить текущий вызов; это не мгновенное убийство pod."),
    "pause_task": (3, "Приостановить обработку задачи", "Без тела запроса. Пауза относится ко всей задаче: новые jobs временно откладываются. Уже выполняющийся solver не прерывается мгновенно. Данные не удаляются. Ответ — текущий план; для продолжения используйте resume."),
    "resume_task": (3, "Снять паузу с задачи", "Без тела запроса. Снимает paused и разрешает обработку ожидающих jobs. Не восстанавливает полностью отменённую задачу, не создаёт заново упавшие jobs и не пересчитывает сохранённые метрики автоматически. Ответ — план."),
}

PARAMETERS = {
    "task_id": "Идентификатор задачи из ответа загрузки (32 шестнадцатеричных символа).",
    "component_id": "ID компонента выбранного анализа; -1 — whole, весь контур.",
    "solution_id": "Идентификатор конкретного решения из списка solutions.",
    "n": "Число зон N, для которого читается сохранённый результат.",
    "overlay": "0 — исходное состояние без изменений. Положительный id — применить журнал до этого события включительно; не последняя ревизия автоматически.",
    "smooth": "false — raw; true — сглаженные нагрузки smooth. Если не передан, используется raw.",
    "start": "true — импорт, подготовка и автоматический расчёт; false — только импорт raw/smooth, затем запускайте /n вручную.",
    "scan_mode": "Перекрывает config.scan_mode: requested — только запрошенные допустимые N, hard — весь диапазон 1..max_useful_n каждого компонента.",
    "whole": "Перекрывает config.whole: дополнительно считать весь контур без компонентной декомпозиции.",
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
    "whole": "Дополнительно рассчитывать whole — весь контур без декомпозиции.",
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
    "id": "Положительный клиентский ID события; уникален внутри задачи. Порядок задаётся append, а не числом id.",
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
        schema["info"].update(title="A101 — раскладка дополнительного армирования", description=INTRO)
        schema["tags"] = deepcopy(TAGS)
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
                op.update(summary=summary, description=description, tags=[TAGS[group]["name"]])
                for p in op.get("parameters", []):
                    name = p["name"]
                    desc = PARAMETERS.get(name)
                    if name == "smooth" and route.name == "list_solutions":
                        desc = "true — smooth, false — raw; если не задан, возвращаются оба варианта."
                    elif name == "smooth" and route.name in {"list_results", "get_result", "get_result_dxf"}:
                        desc = "true — smooth, false — raw; если не задан, используется initial_variant задачи."
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
        decorated = True
        return schema

    app.openapi = openapi
