import asyncio
import json
from collections.abc import AsyncGenerator, Generator
from typing import Any

from django.http import StreamingHttpResponse


def sse_response(generator: Generator[tuple[str, Any], None, None]) -> StreamingHttpResponse:
    def stream() -> Generator[str, None, None]:
        for event, data in generator:
            yield f"event: {event}\n"
            yield f"data: {json.dumps(data)}\n\n"

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


async def keepalive_generator(real_generator: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
    async for event in real_generator:
        yield event
    while True:
        await asyncio.sleep(25)
        yield ": keepalive\n\n"
