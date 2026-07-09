"""A tiny helper to poll blocking hardware reads off the Qt GUI thread.

Widgets that periodically read device state (stage positions, autofocus telemetry,
...) traditionally do so on a ``QTimer`` that fires on the GUI thread and calls
blocking ``getProperty`` / ``getXYPosition`` style methods.  Each of those is a
serial round-trip to the controller, so while it runs the Qt event loop is stalled
and anything else driven by the event loop -- most visibly the live-camera preview
-- freezes.

:class:`AsyncPoller` keeps the periodic ``QTimer`` but moves the *read* onto a
background worker thread (via :func:`superqt.utils.create_worker`) and applies the
result back on the GUI thread, so the event loop is never blocked.  An in-flight
guard ensures a slow read never lets workers pile up: if the previous read has not
finished, the tick is skipped.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Callable, Generic, TypeVar

from qtpy.QtCore import QObject, QTimer
from superqt.utils import create_worker

if TYPE_CHECKING:
    from superqt.utils import FunctionWorker

_T = TypeVar("_T")


class AsyncPoller(QObject, Generic[_T]):
    """Periodically run a blocking ``read`` off-thread and ``apply`` it on-thread.

    Parameters
    ----------
    read : Callable[[], _T]
        Runs on a background worker thread each tick.  Should perform the blocking
        hardware reads and return a plain snapshot of values.  **Must not touch any
        Qt widget** (widget access is only safe on the GUI thread).  It should not
        raise for expected conditions; any exception is swallowed and the tick is
        dropped.
    apply : Callable[[_T], None]
        Runs on the GUI thread with the value returned by ``read``.  This is where
        widgets are updated.
    interval_ms : int
        Polling interval in milliseconds.
    parent : QObject | None
        Optional parent (typically the owning widget, for lifetime management).
    active : Callable[[], bool] | None
        Optional predicate evaluated on the GUI thread before each *timer* tick; if
        it returns ``False`` the tick is skipped (e.g. ``widget.isVisible`` to avoid
        polling a hidden tab).  A ``force`` call to :meth:`poll_now` bypasses it.
    """

    def __init__(
        self,
        read: Callable[[], _T],
        apply: Callable[[_T], None],
        *,
        interval_ms: int,
        parent: QObject | None = None,
        active: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._read = read
        self._apply = apply
        self._active = active
        self._worker: FunctionWorker | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(int(interval_ms))
        self._timer.timeout.connect(self.poll_now)

    def start(self) -> None:
        """Begin periodic polling."""
        self._timer.start()

    def stop(self) -> None:
        """Stop periodic polling (any in-flight read still completes)."""
        self._timer.stop()

    def is_running(self) -> bool:
        """Whether the periodic timer is currently active."""
        return self._timer.isActive()

    def set_interval(self, interval_ms: int) -> None:
        """Change the polling interval, in milliseconds."""
        self._timer.setInterval(int(interval_ms))

    def poll_now(self, force: bool = False) -> None:
        """Kick a single read now (unless one is already in flight).

        Parameters
        ----------
        force : bool
            If ``True``, ignore the ``active`` predicate (used for one-off refreshes
            such as construction or after a user action, where the widget may not be
            visible yet).  The in-flight guard is always respected.
        """
        if self._worker is not None:
            return  # a previous read is still running; skip to avoid pile-up
        if not force and self._active is not None and not self._active():
            return
        self._worker = create_worker(
            self._read,
            _start_thread=True,
            _connect={
                "returned": self._on_returned,
                "errored": self._on_errored,
                "finished": self._on_finished,
            },
        )

    def _on_returned(self, result: _T) -> None:
        # Runs on the GUI thread. The owning widget may have been destroyed while the
        # read was in flight, in which case touching it raises RuntimeError -> ignore.
        with suppress(RuntimeError):
            self._apply(result)

    def _on_errored(self, _exc: Exception) -> None:
        # Swallow hardware read errors (parity with the suppress-based read paths);
        # connecting this also prevents create_worker from re-raising on the GUI thread.
        pass

    def _on_finished(self) -> None:
        self._worker = None
