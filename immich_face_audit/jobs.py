"""One background job at a time (extract, baseline, score, apply, undo),
with a log and a progress counter the web app polls."""
from __future__ import annotations

import threading
import time
import traceback
import uuid


class Busy(RuntimeError):
    pass


class Job:
    def __init__(self, kind: str) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.state = "running"
        self.lines: list[str] = []
        self.progress: tuple[int, int] | None = None
        self.result = None
        self.error: str | None = None
        self.started = time.time()
        self.ended: float | None = None

    # passed to the pipeline functions as their `log` / `progress` callbacks
    def log(self, *parts) -> None:
        self.lines.append(" ".join(str(p) for p in parts))
        del self.lines[:-500]

    def set_progress(self, done: int, total: int) -> None:
        self.progress = (done, total)

    def snapshot(self) -> dict:
        return {"id": self.id, "kind": self.kind, "state": self.state, "log": self.lines[-200:],
                "progress": self.progress, "result": self.result, "error": self.error,
                "started": self.started, "ended": self.ended}


class Jobs:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: Job | None = None

    def running(self) -> bool:
        return bool(self.current and self.current.state == "running")

    def start(self, kind: str, fn) -> dict:
        """Run fn(job) in a thread; refuse if another job is still running."""
        with self._lock:
            if self.running():
                raise Busy(f"{self.current.kind} is still running")
            job = self.current = Job(kind)
        threading.Thread(target=self._run, args=(job, fn), daemon=True).start()
        return job.snapshot()

    @staticmethod
    def _run(job: Job, fn) -> None:
        try:
            job.result = fn(job)
            job.state = "done"
        except SystemExit as e:  # the pipeline's "can't continue" errors
            job.error, job.state = str(e), "error"
        except Exception as e:
            traceback.print_exc()
            job.error, job.state = f"{type(e).__name__}: {e}", "error"
        job.ended = time.time()

    def snapshot(self) -> dict | None:
        return self.current.snapshot() if self.current else None
