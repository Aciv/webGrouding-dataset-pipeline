"""JSONL transport to one persistent Node/Chromium executor, with owned-process cleanup."""
import json
import os
import queue
import signal
import subprocess
import threading
from pathlib import Path


class BrowserExecutorFailure(ValueError):
    """Transport/browser failure carries a recovery category, unlike invalid input or disk errors."""
    def __init__(self, message, category="process_exit"):
        super().__init__(message)
        self.category = category


def memory_status():
    """Optional Linux memory snapshot at failure detection; not proof of an OOM cause."""
    try:
        lines = Path("/proc/meminfo").read_text().splitlines()
        values = {line.split(":", 1)[0]: int(line.split()[1]) / 1024 for line in lines if ":" in line}
        return {key: round(values[key], 1) for key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree") if key in values}
    except (OSError, ValueError):
        return {}


class BrowserWorker:
    """Drain stdout continuously so diagnostic output cannot block browser execution."""
    def __init__(self, project, concurrency, timeout_seconds):
        self.events = queue.Queue()
        self.owned_groups = {}
        self.failure = None
        self.closing = False
        self.child = subprocess.Popen(
            ["node", str(project / "dist/application/discovery/stream-worker.js"),
             str(concurrency), str(timeout_seconds)], cwd=project,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        """Queue full lines and a distinct EOF marker, avoiding select/TextIO buffering traps."""
        for line in self.child.stdout:
            if '"type":"stream_ready"' in line:
                self._remember_groups()
            if '"type":"stream_fatal"' in line and not self.closing:
                event = json.loads(line)
                self.failure = BrowserExecutorFailure(event.get("error", "Executor failed"), event.get("category", "executor_error"))
            self.events.put(("line", line))
        if not self.closing and self.failure is None:
            self.failure = BrowserExecutorFailure("Browser executor output closed unexpectedly")
        self.events.put(("eof", None))

    def _remember_groups(self):
        """Track this executor's Chromium groups, which Playwright may launch detached (Linux)."""
        try:
            rows = subprocess.check_output(["ps", "-eo", "pid=,ppid="], text=True)
            processes = [tuple(map(int, line.split())) for line in rows.splitlines()]
            owned = {self.child.pid}
            while True:
                children = {pid for pid, parent in processes if parent in owned}
                if children <= owned:
                    break
                owned |= children
            for pid in owned:
                try:
                    group = os.getpgid(pid)
                    if group != os.getpgrp():
                        # Preserve the leader's start time so PID reuse can never target unrelated work.
                        self.owned_groups[group] = Path(f"/proc/{group}/stat").read_text().rsplit(")", 1)[1].split()[19]
                except (OSError, IndexError):
                    pass
        except (OSError, subprocess.SubprocessError):
            pass

    def send(self, value):
        """Flush a small control record; an exited executor is a resumable process failure."""
        if self.failure:
            raise self.failure
        if self.child.poll() is not None:
            raise BrowserExecutorFailure(f"Browser executor exited: {self.child.returncode}")
        try:
            self.child.stdin.write(json.dumps(value) + "\n")
            self.child.stdin.flush()
        except (OSError, ValueError) as error:
            raise BrowserExecutorFailure(f"Browser executor input closed: {error}") from error

    def close(self, interrupt=False):
        """Reap Node and only its own browser process group, escalating after a grace period."""
        self.closing = True
        if self.child.poll() is None:
            try:
                if interrupt:
                    self.child.send_signal(signal.SIGINT)
                else:
                    self.send({"type": "shutdown"})
                self.child.stdin.close()
                self.child.wait(timeout=12)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.child.wait()
        self.reader.join(timeout=2)
        for group, started in self.owned_groups.items():
            try:
                current = Path(f"/proc/{group}/stat").read_text().rsplit(")", 1)[1].split()[19]
                if current == started:
                    os.killpg(group, signal.SIGKILL)
            except (OSError, IndexError):
                pass
        if not self.child.stdin.closed:
            try:
                self.child.stdin.close()
            except OSError:
                pass
        self.child.stdout.close()
