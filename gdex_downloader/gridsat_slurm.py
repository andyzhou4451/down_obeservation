"""Small, fail-closed Slurm adapter for the quarterly GridSat controller."""

from __future__ import annotations

import getpass
import math
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path


_COMMAND_TIMEOUT_SECONDS = 60
_ACCOUNTING_POLLS = 10
_MAX_WAIT_SECONDS = 7 * 24 * 60 * 60
_SUBMIT_ENVIRONMENT = {
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LD_LIBRARY_PATH",
    "SLURM_CONF", "SLURM_CONF_SERVER", "SLURM_JWT", "XDG_RUNTIME_DIR",
    "SYSTEMROOT", "SystemRoot", "WINDIR",
}
_ACTIVE_STATES = {
    "PENDING", "RUNNING", "SUSPENDED", "CONFIGURING", "COMPLETING",
    "RESIZING", "REQUEUED", "REQUEUE_FED", "REQUEUE_HOLD", "SPECIAL_EXIT",
    "SIGNALING", "STAGE_OUT", "STOPPED", "POWER_UP_NODE",
}
_FAILED_STATES = {
    "BOOT_FAIL", "CANCELLED", "DEADLINE", "FAILED", "NODE_FAIL",
    "OUT_OF_MEMORY", "PREEMPTED", "REVOKED", "TIMEOUT",
}


class JobFailedError(RuntimeError):
    """Slurm confirmed a terminal failure, so a new attempt may be scheduled."""


class SlurmScheduler:
    def __init__(
        self,
        app_dir: Path,
        data_root: Path,
        python_launcher: str,
        poll_seconds: float = 30.0,
        workers: int = 2,
    ) -> None:
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be finite and positive")
        if type(workers) is not int or not 1 <= workers <= 2:
            raise ValueError("workers must be 1 or 2 for the two-CPU Slurm allocation")
        if not python_launcher or "\0" in python_launcher:
            raise ValueError("python_launcher must name one executable")
        self.app_dir = Path(app_dir).resolve()
        self.data_root = Path(data_root).resolve()
        self.python_launcher = python_launcher
        self.poll_seconds = poll_seconds
        self.workers = workers

    def find_job(self, job_name: str) -> str | None:
        """Find one exact name in the queue and the last 30 days of accounting.

        Completed and failed jobs are returned too; pass their IDs to ``wait``.
        None is not proof that a previous submission never happened. A caller
        with a persisted submission intent must fail closed, not resubmit it.
        """
        self._validate_job_name(job_name)
        matches: set[str] = set()
        for job_id, name, _state in self._queue(job_name):
            if name == job_name:
                self._validate_job_id(job_id)
                matches.add(job_id)
        if len(matches) > 1:
            raise RuntimeError(f"Multiple Slurm jobs match {job_name!r}: {sorted(matches)}")
        for job_id, name, _state, _exit_code in self._accounting(f"--name={job_name}"):
            if name == job_name and "." not in job_id:
                self._validate_job_id(job_id)
                matches.add(job_id)
        if len(matches) > 1:
            raise RuntimeError(f"Multiple Slurm jobs match {job_name!r}: {sorted(matches)}")
        return next(iter(matches), None)

    def submit(self, quarter: str, job_name: str) -> str:
        """Submit once; the caller must persist job_name before calling this."""
        if not re.fullmatch(r"[0-9]{4}Q[1-4]", quarter):
            raise ValueError("quarter must have the form 2016Q1")
        self._validate_job_name(job_name)
        log_dir = self.data_root / "_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_prefix = str(log_dir / f"gridsat-{quarter}").replace("%", "%%")
        result = self._run([
            "sbatch", "--parsable", f"--job-name={job_name}",
            f"--chdir={self.app_dir}", f"--output={log_prefix}-%j.out",
            f"--error={log_prefix}-%j.err", "--partition=5090",
            "--nodes=1", "--ntasks=1", "--cpus-per-task=2", "--mem=8G",
            "--time=2-00:00:00", "--no-requeue", "--export=ALL",
            str(self.app_dir / "deploy" / "th-hpc6-gridsat-worker.sbatch"),
            str(self.app_dir), str(self.data_root), self.python_launcher,
            quarter, str(self.workers),
        ])
        match = re.fullmatch(r"([1-9][0-9]*)(?:;[A-Za-z0-9_.-]+)?", result.strip())
        if match is None:
            raise RuntimeError(
                f"Unrecognized sbatch job ID: {result!r}; reconcile {job_name!r} "
                "before retrying submission"
            )
        return match.group(1)

    def wait(self, job_id: str, heartbeat: Callable[[], None] | None = None) -> None:
        """Wait for COMPLETED with exit 0:0, never merely an empty queue.

        Missing or stale accounting is tolerated for ten polls. The overall
        seven-day deadline bounds queue waits without cancelling the job.
        Uncertain outcomes raise RuntimeError, not JobFailedError.
        """
        self._validate_job_id(job_id)
        deadline = time.monotonic() + _MAX_WAIT_SECONDS
        accounting_polls = 0
        while True:
            if heartbeat is not None:
                heartbeat()
            # Filtering locally avoids squeue's "invalid job id" error after
            # Slurm has purged a completed allocation from its live queue.
            queued = [row for row in self._queue() if row[0] == job_id]
            if len(queued) > 1:
                raise RuntimeError(f"Multiple queue records for Slurm job {job_id}")
            state = self._state(queued[0][2]) if queued else None
            if state in _FAILED_STATES:
                raise JobFailedError(f"Slurm job {job_id} ended in {state}")
            if state is not None and state not in _ACTIVE_STATES | {"COMPLETED"}:
                raise RuntimeError(f"Unknown queue state for Slurm job {job_id}: {state!r}")
            if state in _ACTIVE_STATES:
                accounting_polls = 0
            else:
                records = [row for row in self._accounting(f"--jobs={job_id}")
                           if row[0] == job_id]
                if len(records) > 1:
                    raise RuntimeError(f"Multiple accounting records for Slurm job {job_id}")
                if records:
                    _, _, raw_state, exit_code = records[0]
                    state = self._state(raw_state)
                    if state in _FAILED_STATES:
                        raise JobFailedError(
                            f"Slurm job {job_id} ended in {state} (exit {exit_code})"
                        )
                    if state == "COMPLETED" and re.fullmatch(r"[0-9]+:[0-9]+", exit_code):
                        if any(int(part) for part in exit_code.split(":")):
                            raise JobFailedError(
                                f"Slurm job {job_id} COMPLETED with nonzero exit {exit_code}"
                            )
                        return
                    if state not in _ACTIVE_STATES | {"COMPLETED", ""}:
                        raise RuntimeError(
                            f"Unknown accounting state for Slurm job {job_id}: {raw_state!r}"
                        )
                accounting_polls += 1
                if accounting_polls >= _ACCOUNTING_POLLS:
                    raise RuntimeError(
                        f"Slurm job {job_id} has no conclusive accounting result after "
                        f"{_ACCOUNTING_POLLS} polls; keep the existing job ID for reconciliation"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"Timed out waiting for Slurm job {job_id}; it may still be active"
                )
            time.sleep(min(self.poll_seconds, remaining))

    def _queue(self, job_name: str | None = None) -> list[list[str]]:
        command = [
            "squeue", "--noheader", "--local", f"--user={getpass.getuser()}",
            "--states=all", "--format=%i|%j|%T",
        ]
        if job_name is not None:
            command.append(f"--name={job_name}")
        return self._rows(self._run(command), 3, "squeue")

    def _accounting(self, *filters: str) -> list[list[str]]:
        return self._rows(self._run([
            "sacct", "--noheader", "--parsable2", "--allocations", "--local",
            f"--user={getpass.getuser()}", "--starttime=now-30days",
            "--format=JobIDRaw,JobName%128,State%64,ExitCode", *filters,
        ]), 4, "sacct")

    def _run(self, command: list[str]) -> str:
        # Login profiles must not inject GPU requests or restrict query results.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("SBATCH_", "SQUEUE_", "SACCT_"))
               and key not in {"SLURM_CLUSTERS", "SLURM_EXPORT_ENV"}}
        if command[0] == "sbatch":
            # Slurm 20 lacks export=NIL; NONE triggers a compute login profile.
            # ALL with a small allowlist works on both old and current Slurm.
            env = {key: value for key, value in env.items() if key in _SUBMIT_ENVIRONMENT}
        env["LC_ALL"] = "C"
        try:
            result = subprocess.run(
                command, cwd=self.app_dir, env=env, check=True,
                capture_output=True, text=True, timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            raise RuntimeError(f"{command[0]} failed ({exc.returncode}): {detail}") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"{command[0]} could not complete: {exc}") from exc
        return result.stdout

    @staticmethod
    def _rows(output: str, columns: int, command: str) -> list[list[str]]:
        rows = []
        for line in output.splitlines():
            if not line.strip():
                continue
            fields = [field.strip() for field in line.split("|")]
            if len(fields) != columns:
                raise RuntimeError(f"Malformed {command} output: {line!r}")
            rows.append(fields)
        return rows

    @staticmethod
    def _state(value: str) -> str:
        return value.split()[0].rstrip("+") if value.strip() else ""

    @staticmethod
    def _validate_job_name(job_name: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", job_name):
            raise ValueError("job_name must be 1-128 letters, digits, dots, underscores or hyphens")

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not re.fullmatch(r"[1-9][0-9]*", job_id):
            raise RuntimeError(f"Invalid single-allocation Slurm job ID: {job_id!r}")
