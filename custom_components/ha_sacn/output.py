"""Coalesce and rate-limit Home Assistant calls per patched entity."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)
ServiceCall = tuple[str, str, dict[str, Any]]


class Output:
    """Keep the latest desired value, serializing calls for each entity."""

    def __init__(self, interval: float, call: Callable[[str, str, dict[str, Any]], Awaitable[None]]) -> None:
        self._interval = interval
        self._call = call
        self._desired: dict[str, ServiceCall] = {}
        self._sent: dict[str, ServiceCall] = {}
        self._generation: dict[str, int] = {}
        self._last_attempt: dict[str, float] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    def submit(self, entity: str, call: ServiceCall) -> None:
        """Replace pending state and schedule its delivery."""
        if self._closed:
            return
        if entity not in self._tasks and call == self._sent.get(entity):
            return
        self._desired[entity] = call
        if entity not in self._tasks:
            self._tasks[entity] = asyncio.create_task(self._run(entity))

    def invalidate(self, entity: str) -> None:
        """Forget pending and sent state when the source or footprint is lost."""
        self._desired.pop(entity, None)
        self._sent.pop(entity, None)
        self._generation[entity] = self._generation.get(entity, 0) + 1

    async def _run(self, entity: str) -> None:
        try:
            while entity in self._desired:
                if self._desired[entity] == self._sent.get(entity):
                    self._desired.pop(entity)
                    continue
                loop = asyncio.get_running_loop()
                delay = self._last_attempt.get(entity, float("-inf")) + self._interval - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                if entity not in self._desired:
                    continue
                call = self._desired.pop(entity)
                if call == self._sent.get(entity):
                    continue
                generation = self._generation.get(entity, 0)
                self._last_attempt[entity] = loop.time()
                try:
                    await self._call(*call)
                except Exception:
                    _LOGGER.exception("sACN service call failed for %s", entity)
                else:
                    if generation == self._generation.get(entity, 0):
                        self._sent[entity] = call
        finally:
            self._tasks.pop(entity, None)

    async def close(self) -> None:
        """Cancel and await all pending and in-flight work."""
        self._closed = True
        self._desired.clear()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
