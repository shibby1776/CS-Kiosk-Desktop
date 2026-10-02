"""Thread-safe pub/sub used to marshal worker-thread events onto the Tk main thread.

All worker threads (serial reader, camera grabber, HTTP worker) push (topic, payload)
tuples onto a single Queue. The Tk main loop polls it via root.after().
"""
from __future__ import annotations

import queue
from collections import defaultdict
from typing import Any, Callable


class EventBus:
    def __init__(
        self,
        error_handler: Callable[[str, Exception], None] | None = None,
    ) -> None:
        self._q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._subs: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self._error_handler = error_handler

    def subscribe(self, topic: str, handler: Callable[[Any], None]) -> None:
        self._subs[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Callable[[Any], None]) -> None:
        if handler in self._subs.get(topic, []):
            self._subs[topic].remove(handler)

    def post(self, topic: str, payload: Any = None) -> None:
        """Called from any thread."""
        self._q.put((topic, payload))

    def drain(self, max_items: int = 64) -> int:
        """Called from the Tk main thread. Returns count dispatched."""
        count = 0
        while count < max_items:
            try:
                topic, payload = self._q.get_nowait()
            except queue.Empty:
                break
            for handler in list(self._subs.get(topic, [])):
                try:
                    handler(payload)
                except Exception as exc:
                    if self._error_handler is not None:
                        try:
                            self._error_handler(topic, exc)
                        except Exception:
                            pass
            count += 1
        return count


def post_assignment_changed(
    bus: EventBus,
    source: str,
    change: dict[str, Any] | None = None,
) -> None:
    """Notify Run surfaces that authoritative routing state changed.

    ``change`` carries optional render hints only. Routing consumers continue
    to read the persisted Config state, so dropping or delaying a UI event can
    never change the destination used by the sorter.
    """
    payload: dict[str, Any] = {"source": source}
    if change:
        payload.update(change)
    bus.post("run/assignment_changed", payload)


def plan_assignment_refresh(
    payload: dict[str, Any] | None,
    *,
    visible: bool,
    local_source: str,
    force: bool = False,
) -> tuple[str, set[int] | None]:
    """Return the smallest safe UI refresh for an assignment event.

    The action is ``defer`` for a hidden surface, ``skip`` when the originating
    surface already rendered the click, ``targeted`` for one slot card, or
    ``full`` for structural changes and visibility transitions.
    """
    if force:
        return "full", None
    if not visible:
        return "defer", None

    source = str((payload or {}).get("source", ""))
    if source == local_source or (
        local_source.endswith("_") and source.startswith(local_source)
    ):
        return "skip", None
    if bool((payload or {}).get("full_refresh", True)):
        return "full", None

    slot = (payload or {}).get("slot")
    return "targeted", {slot} if isinstance(slot, int) else None
