"""Cancellation-safe ownership of finite cleanup operations."""

from __future__ import annotations

import asyncio
import logging
from typing import TypeVar

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")


async def async_wait_cleanup(task: asyncio.Task[_T]) -> _T:
    """Honor caller cancellation only after the owned cleanup has finished."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        drained = asyncio.gather(task, return_exceptions=True)
        while not drained.done():
            try:
                await asyncio.shield(drained)
            except asyncio.CancelledError:
                continue
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOGGER.error(
                "Cleanup failed while handling cancellation (%s)", type(error).__name__
            )
        raise
