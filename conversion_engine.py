from __future__ import annotations

import queue
import threading
import time
from typing import Callable

import utils

class ConversionEngine:
    """Background conversion queue + worker threads.

    UI code should enqueue tasks and poll events via get_event_nowait().
    """

    def __init__(
        self,
        *,
        output_dir_getter: Callable[[], str],
        debug_log: Callable[[str], None],
    ) -> None:
        self._output_dir_getter = output_dir_getter
        self._debug_log = debug_log

        self._pending_tasks: list[tuple[str, str]] = []
        self._pending_cv = threading.Condition()
        self._ui_events: queue.Queue[tuple] = queue.Queue()
        self._shutdown = threading.Event()

        # Parallel execution control (sequential by default).
        self._parallel_limit: int = 1
        self._active_conversions: int = 0
        # One-shot parallel: enabled when user drops onto the current job.
        # Reverts to sequential as soon as one of the parallel jobs finishes.
        self._parallel_one_shot: bool = False
        self._parallel_engaged: bool = False

        self._worker: threading.Thread | None = None
        self._extra_worker: threading.Thread | None = None

    @property
    def shutdown_event(self) -> threading.Event:
        return self._shutdown

    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._conversion_worker, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        try:
            self._shutdown.set()
        except Exception:
            pass
        try:
            with self._pending_cv:
                self._pending_cv.notify_all()
        except Exception:
            pass

    def enqueue(self, src_path: str, target_ext: str) -> None:
        with self._pending_cv:
            self._pending_tasks.append((src_path, target_ext))
            self._pending_cv.notify_all()

    def remove_pending(self, paths: set[str]) -> None:
        try:
            with self._pending_cv:
                self._pending_tasks = [(p, t) for (p, t) in self._pending_tasks if p not in paths]
                self._pending_cv.notify_all()
        except Exception:
            pass

    def reorder_pending(self, from_idx: int, to_idx: int) -> None:
        with self._pending_cv:
            if from_idx < 0 or to_idx < 0:
                return
            if from_idx >= len(self._pending_tasks) or to_idx >= len(self._pending_tasks):
                return
            item = self._pending_tasks.pop(from_idx)
            self._pending_tasks.insert(to_idx, item)
            self._pending_cv.notify_all()

    def pending_snapshot(self) -> list[tuple[str, str]]:
        try:
            with self._pending_cv:
                return list(self._pending_tasks)
        except Exception:
            return []

    def worker_stats(self) -> tuple[int, int]:
        """Return (pending_count, active_workers)."""
        try:
            with self._pending_cv:
                return len(self._pending_tasks), int(self._active_conversions)
        except Exception:
            return 0, 0

    def worker_limit(self) -> int:
        try:
            with self._pending_cv:
                return int(self._parallel_limit)
        except Exception:
            return 1

    def get_event_nowait(self) -> tuple:
        return self._ui_events.get_nowait()

    def enable_parallel_one_shot(self) -> None:
        """Enable 2-way parallel processing for the current batch."""
        try:
            extra_to_start: threading.Thread | None = None
            with self._pending_cv:
                if self._parallel_limit < 2:
                    self._parallel_limit = 2
                self._parallel_one_shot = True
                self._parallel_engaged = False

                if self._extra_worker is None or (not self._extra_worker.is_alive()):
                    extra_to_start = threading.Thread(
                        target=self._conversion_worker,
                        kwargs={"is_extra": True},
                        daemon=True,
                    )
                    self._extra_worker = extra_to_start
                self._pending_cv.notify_all()

            if extra_to_start is not None:
                extra_to_start.start()
        except Exception:
            pass

    def revert_to_sequential_if_idle(self, *, in_progress_empty: bool) -> None:
        """Revert to sequential mode once the whole batch is done."""
        try:
            with self._pending_cv:
                if in_progress_empty and not self._pending_tasks:
                    self._parallel_limit = 1
                    self._parallel_one_shot = False
                    self._parallel_engaged = False
                    self._pending_cv.notify_all()
        except Exception:
            pass

    def _conversion_worker(self, is_extra: bool = False) -> None:
        while True:
            with self._pending_cv:
                while True:
                    if self._shutdown.is_set():
                        try:
                            who = "extra" if is_extra else "primary"
                            self._debug_log(f"WORKER {who} exit: shutdown")
                        except Exception:
                            pass
                        return

                    # Extra worker self-terminates whenever we return to sequential mode.
                    if is_extra and self._parallel_limit <= 1:
                        try:
                            self._debug_log("WORKER extra exit: sequential mode")
                        except Exception:
                            pass
                        return

                    if self._pending_tasks and (self._active_conversions < self._parallel_limit):
                        src_path, target_ext = self._pending_tasks.pop(0)
                        self._active_conversions += 1
                        if self._parallel_limit > 1 and self._parallel_one_shot and self._active_conversions >= 2:
                            self._parallel_engaged = True
                        break

                    self._pending_cv.wait()

            try:
                self._ui_events.put(("start", src_path, target_ext))

                # Predict output paths so the user can find ffmpeg logs even if the job hangs.
                expected_out = None
                expected_ffmpeg_log = None
                try:
                    preview = utils.FileConverter(self._output_dir_getter())
                    preview.ensureOutputPath()
                    expected_out = preview.buildOutputPath(src_path, target_ext)
                    expected_ffmpeg_log = expected_out + ".ffmpeg.log"
                except Exception:
                    pass

                self._debug_log(
                    f"WORKER start: src={src_path} target={target_ext}"
                    + (f" out={expected_out}" if expected_out else "")
                    + (f" ffmpeg_log={expected_ffmpeg_log}" if expected_ffmpeg_log else "")
                )

                last_logged = 0.0
                last_log_t = time.time()

                def _progress_cb(v: float) -> None:
                    self._ui_events.put(("progress", src_path, float(v)))
                    nonlocal last_logged, last_log_t
                    now = time.time()
                    vv = float(v)
                    if vv >= 1.0 or (vv - last_logged) >= 0.05 or (now - last_log_t) >= 5.0:
                        last_logged = max(last_logged, vv)
                        last_log_t = now
                        self._debug_log(f"WORKER progress: src={src_path} v={vv:.3f}")

                cancel_cb = (lambda: bool(self._shutdown.is_set()))
                out_path = utils.convertFile(src_path, target_ext, self._output_dir_getter(), progress=_progress_cb, cancel=cancel_cb)
                self._ui_events.put(("done", src_path, out_path, target_ext))
                self._debug_log(f"WORKER done: src={src_path} out={out_path}")

            except utils.ConversionCancelled as e:
                if self._shutdown.is_set():
                    self._debug_log(f"WORKER cancelled during shutdown: src={src_path} msg={e}")
                    return
                self._ui_events.put(("error", src_path, str(e)))

            except Exception as e:
                self._ui_events.put(("error", src_path, str(e)))
                self._debug_log(f"WORKER error: src={src_path} err={e}")

            finally:
                try:
                    with self._pending_cv:
                        self._active_conversions = max(0, int(self._active_conversions) - 1)

                        # One-shot parallel ends as soon as one of the parallel jobs finishes,
                        # leaving at most one active conversion.
                        if (
                            self._parallel_limit > 1
                            and self._parallel_one_shot
                            and self._parallel_engaged
                            and int(self._active_conversions) <= 1
                        ):
                            self._parallel_limit = 1
                            self._parallel_one_shot = False
                            self._parallel_engaged = False
                            try:
                                self._debug_log("PARALLEL one-shot ended; reverting to sequential")
                            except Exception:
                                pass
                        self._pending_cv.notify_all()
                except Exception:
                    pass
