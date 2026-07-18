"""首次设置阶段的后台任务与可查询进度。"""
from __future__ import annotations

import copy
import threading
import time
import uuid


class JobRegistry:
    def __init__(self):
        self._jobs = {}
        self._events = {}
        self._lock = threading.Lock()

    def start(self, kind, operation):
        job_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id, "kind": str(kind), "status": "queued",
                "stage": "queued", "message": "正在开始…", "progress": 0,
                "processed": 0, "result": None, "error": "",
                "created_at": now, "updated_at": now,
            }
            self._events[job_id] = threading.Event()
        thread = threading.Thread(
            target=self._run, args=(job_id, operation), daemon=True,
            name=f"shiguang-{kind}-{job_id[:8]}",
        )
        thread.start()
        return job_id

    def _update(self, job_id, **values):
        with self._lock:
            job = self._jobs[job_id]
            for key in ("status", "stage", "message", "processed", "result", "error"):
                if key in values:
                    job[key] = values[key]
            if "progress" in values:
                job["progress"] = min(100, max(0, int(values["progress"])))
            job["updated_at"] = time.time()

    def _run(self, job_id, operation):
        self._update(job_id, status="running", stage="starting", progress=1,
                     message="正在开始…")
        try:
            result = operation(lambda **values: self._update(job_id, **values))
            self._update(job_id, status="completed", stage="completed", progress=100,
                         message="已完成", result=result)
        except Exception as exc:
            self._update(job_id, status="failed", stage="failed",
                         message="操作没有完成", error=str(exc))
        finally:
            self._events[job_id].set()

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(str(job_id))
            return copy.deepcopy(job) if job else None

    def wait(self, job_id, timeout=None):
        event = self._events.get(str(job_id))
        if event:
            event.wait(timeout)
        return self.get(job_id)

