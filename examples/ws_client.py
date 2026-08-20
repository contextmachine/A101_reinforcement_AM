import asyncio
import json
import os
import sys

import websockets


async def main(task_id: str):
    base = os.getenv(
        "REBAR_WS_URL",
        "ws://localhost:8000",
    )

    uri = f"{base}/v1/tasks/{task_id}/ws"

    async with websockets.connect(
        uri,
        max_size=None,
    ) as ws:

        await ws.send(
            json.dumps({
                "action": "add",
                "n": [35, 45],
            })
        )

        async for message in ws:
            event = json.loads(message)

            print(
                json.dumps(
                    event,
                    ensure_ascii=False,
                    indent=2,
                )
            )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))