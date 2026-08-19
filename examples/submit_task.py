import json
import os
import sys
from pathlib import Path

import httpx

base = os.getenv("REBAR_API_URL", "http://localhost:8000")
key = os.getenv("REBAR_API_KEY", "dev-api-key")
path = Path(sys.argv[1] if len(sys.argv) > 1 else "examples/task_polygons.json")
with httpx.Client(timeout=60) as client:
    response = client.post(
        f"{base}/v1/tasks",
        headers={"x-api-key": key},
        json=json.loads(path.read_text(encoding="utf-8")),
    )
    response.raise_for_status()
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))
