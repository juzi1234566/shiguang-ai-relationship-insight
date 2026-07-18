"""Browser-tab lifecycle support for the distributable local web app."""
import threading


class BrowserLifecycle:
    def __init__(self, on_close, *, grace_seconds=20, idle_seconds=90,
                 timer_factory=threading.Timer):
        self._on_close = on_close
        self._grace_seconds = grace_seconds
        self._idle_seconds = idle_seconds
        self._timer_factory = timer_factory
        self._timer = None
        self._generation = 0
        self._lock = threading.Lock()

    def touch(self):
        with self._lock:
            self._generation += 1
            token = self._generation
            if self._timer:
                self._timer.cancel()
            self._timer = self._timer_factory(
                self._idle_seconds, lambda: self._fire(token)
            )
            self._timer.daemon = True
            timer = self._timer
        timer.start()

    def request_close(self):
        with self._lock:
            self._generation += 1
            token = self._generation
            if self._timer:
                self._timer.cancel()
            self._timer = self._timer_factory(
                self._grace_seconds, lambda: self._fire(token)
            )
            self._timer.daemon = True
            timer = self._timer
        timer.start()

    def _fire(self, token):
        with self._lock:
            if token != self._generation:
                return
            self._timer = None
        self._on_close()
