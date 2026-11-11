from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gdex_downloader import gridsat_slurm
from gdex_downloader.gridsat_slurm import JobFailedError, SlurmScheduler


REPO = Path(__file__).resolve().parents[1]
JOB_NAME = "gridsat_root_2016Q1_attempt1"
LAUNCHER = "/fs6/home/yangjh18/gdas_healda_20260916/runtime/science-python.sh"
BASH = shutil.which("bash")
if BASH is None and Path("C:/Program Files/Git/bin/bash.exe").is_file():
    BASH = "C:/Program Files/Git/bin/bash.exe"


def response(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout, "")


def queue(state: str = "RUNNING", job_id: str = "123", name: str = JOB_NAME) -> str:
    return f"{job_id}|{name}|{state}\n"


def accounting(
    state: str = "COMPLETED", exit_code: str = "0:0",
    job_id: str = "123", name: str = JOB_NAME,
) -> str:
    return f"{job_id}|{name}|{state}|{exit_code}\n"


class SlurmSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app_dir = Path(temp.name) / "application with spaces"
        self.app_dir.mkdir()
        self.data_root = Path(temp.name) / "data with spaces,commas"
        self.scheduler = SlurmScheduler(self.app_dir, self.data_root, LAUNCHER)
        runner = patch("gdex_downloader.gridsat_slurm.subprocess.run")
        self.run = runner.start()
        self.addCleanup(runner.stop)
        sleeper = patch("gdex_downloader.gridsat_slurm.time.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def outputs(self, *outputs: str) -> None:
        self.run.side_effect = [response(output) for output in outputs]

    def assert_uncertain(self, function, *args) -> None:
        with self.assertRaises(RuntimeError) as caught:
            function(*args)
        self.assertNotIsInstance(caught.exception, JobFailedError)

    def test_submit_id_and_exact_worker_arguments(self) -> None:
        self.outputs("123;cluster1\n")
        self.assertEqual(self.scheduler.submit("2016Q1", JOB_NAME), "123")
        args, kwargs = self.run.call_args
        command = args[0]
        self.assertEqual(command[0], "sbatch")
        for option in (
            "--parsable", "--partition=5090", "--nodes=1", "--ntasks=1",
            "--cpus-per-task=2", "--mem=8G", "--time=2-00:00:00", "--no-requeue",
            "--export=ALL", f"--job-name={JOB_NAME}", f"--chdir={self.app_dir}",
            f"--output={self.data_root / '_logs' / 'gridsat-2016Q1-%j.out'}",
            f"--error={self.data_root / '_logs' / 'gridsat-2016Q1-%j.err'}",
        ):
            self.assertIn(option, command)
        self.assertFalse(any(arg.startswith(("--gpu", "--gres")) for arg in command))
        self.assertEqual(command[-6:], [
            str(self.app_dir / "deploy" / "th-hpc6-gridsat-worker.sbatch"),
            str(self.app_dir), str(self.data_root), LAUNCHER, "2016Q1", "2",
        ])
        self.assertTrue((self.data_root / "_logs").is_dir())
        self.assertEqual(kwargs["cwd"], self.app_dir)
        self.assertEqual(kwargs["timeout"], 60)
        self.assertTrue(kwargs["check"])
        self.assertTrue(kwargs["text"])
        self.assertTrue(kwargs["capture_output"])
        self.assertNotIn("shell", kwargs)

    def test_submit_plain_numeric_id_and_one_worker(self) -> None:
        self.outputs("987\n")
        scheduler = SlurmScheduler(self.app_dir, self.data_root, LAUNCHER, workers=1)
        self.assertEqual(scheduler.submit("2026Q4", JOB_NAME), "987")
        self.assertEqual(self.run.call_args.args[0][-2:], ["2026Q4", "1"])

    def test_submit_rejects_malformed_id_without_retry(self) -> None:
        for stdout in ("", "Submitted batch job 123", "123\n456", "0", "123;", "123_1"):
            with self.subTest(stdout=stdout):
                self.run.reset_mock()
                self.outputs(stdout)
                self.assert_uncertain(self.scheduler.submit, "2016Q1", JOB_NAME)
                self.run.assert_called_once()

    def test_submit_does_not_export_login_environment_or_gpu_defaults(self) -> None:
        self.outputs("123")
        with patch.dict(os.environ, {
            "SBATCH_GPUS": "4", "SBATCH_PARTITION": "wrong", "SBATCH_EXPORT": "ALL",
            "SQUEUE_STATES": "RUNNING", "SACCT_FORMAT": "JobName", "SLURM_CLUSTERS": "other",
            "SLURM_EXPORT_ENV": "ALL", "BASH_ENV": "/tmp/profile", "SECRET_TOKEN": "secret",
            "http_proxy": "http://proxy.invalid:8080", "PATH": "/site/bin:/usr/bin",
            "SLURM_CONF": "/site/slurm.conf", "LD_LIBRARY_PATH": "/site/lib",
        }):
            self.scheduler.submit("2016Q1", JOB_NAME)
        env = self.run.call_args.kwargs["env"]
        self.assertEqual(env["PATH"], "/site/bin:/usr/bin")
        self.assertEqual(env["SLURM_CONF"], "/site/slurm.conf")
        self.assertEqual(env["LD_LIBRARY_PATH"], "/site/lib")
        self.assertEqual(env["LC_ALL"], "C")
        for key in ("SBATCH_GPUS", "SBATCH_PARTITION", "SBATCH_EXPORT", "SQUEUE_STATES",
                    "SACCT_FORMAT", "SLURM_CLUSTERS", "SLURM_EXPORT_ENV", "BASH_ENV",
                    "SECRET_TOKEN", "http_proxy"):
            self.assertNotIn(key, env)

    def test_validation_before_submission(self) -> None:
        for quarter in ("2016Q0", "2016Q5", "2016Q1;touch x", "16Q1"):
            with self.subTest(quarter=quarter), self.assertRaises(ValueError):
                self.scheduler.submit(quarter, JOB_NAME)
        for name in ("", "name,another", "name|another", "-name", "x" * 129):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.scheduler.find_job(name)
        self.run.assert_not_called()

    def test_constructor_validation(self) -> None:
        for workers in (0, 3, -1, 1.5, True):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                SlurmScheduler(self.app_dir, self.data_root, LAUNCHER, workers=workers)
        for seconds in (0, -1, float("nan"), float("inf")):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                SlurmScheduler(self.app_dir, self.data_root, LAUNCHER, poll_seconds=seconds)

    def test_find_job_including_pending_suspended_and_completed_queue(self) -> None:
        for state in ("PENDING", "RUNNING", "SUSPENDED", "COMPLETING", "COMPLETED"):
            with self.subTest(state=state):
                self.outputs(queue(state), "")
                self.assertEqual(self.scheduler.find_job(JOB_NAME), "123")
                command = self.run.call_args_list[-2].args[0]
                self.assertIn("--states=all", command)
                self.assertIn(f"--name={JOB_NAME}", command)

    def test_restart_lookup_finds_finished_job_outside_today(self) -> None:
        for state in ("COMPLETED", "FAILED", "CANCELLED by 42"):
            with self.subTest(state=state):
                self.outputs("", accounting(state))
                self.assertEqual(self.scheduler.find_job(JOB_NAME), "123")
                command = self.run.call_args.args[0]
                self.assertEqual(command[0], "sacct")
                self.assertIn("--starttime=now-30days", command)
                self.assertIn("--allocations", command)
                self.assertIn("--parsable2", command)
                self.assertIn("--format=JobIDRaw,JobName%128,State%64,ExitCode", command)

    def test_lookup_deduplicates_queue_and_accounting(self) -> None:
        self.outputs(queue(), accounting("RUNNING"))
        self.assertEqual(self.scheduler.find_job(JOB_NAME), "123")

    def test_lookup_requires_exact_name_and_ignores_job_steps(self) -> None:
        self.outputs(queue(name=JOB_NAME + "_other"),
                     accounting(job_id="123.batch") + accounting(name=JOB_NAME + "_other"))
        self.assertIsNone(self.scheduler.find_job(JOB_NAME))

    def test_lookup_returns_none_only_after_both_successful_queries(self) -> None:
        self.outputs("", "")
        self.assertIsNone(self.scheduler.find_job(JOB_NAME))
        self.assertEqual([call.args[0][0] for call in self.run.call_args_list], ["squeue", "sacct"])

    def test_lookup_rejects_multiple_jobs(self) -> None:
        for outputs in (
            (queue() + queue(job_id="124"),),
            ("", accounting() + accounting(job_id="124")),
            (queue(), accounting(job_id="124")),
        ):
            with self.subTest(outputs=outputs):
                self.outputs(*outputs)
                self.assert_uncertain(self.scheduler.find_job, JOB_NAME)

    def test_lookup_rejects_unsupported_allocation_ids(self) -> None:
        for job_id in ("123_4", "123+1", "n/a"):
            with self.subTest(job_id=job_id):
                self.outputs(queue(job_id=job_id))
                self.assert_uncertain(self.scheduler.find_job, JOB_NAME)

    def test_lookup_query_errors_and_timeouts_fail_closed(self) -> None:
        for error in (
            subprocess.CalledProcessError(1, "squeue", stderr="unavailable"),
            subprocess.TimeoutExpired("squeue", 60), FileNotFoundError("squeue"),
        ):
            with self.subTest(error=error):
                self.run.side_effect = error
                self.assert_uncertain(self.scheduler.find_job, JOB_NAME)
        self.run.side_effect = [response(queue()),
                               subprocess.CalledProcessError(1, "sacct", stderr="database down")]
        self.assert_uncertain(self.scheduler.find_job, JOB_NAME)

    def test_bad_query_output_fails_closed(self) -> None:
        for outputs in (("unexpected output",), ("", "123|COMPLETED")):
            with self.subTest(outputs=outputs):
                self.outputs(*outputs)
                self.assert_uncertain(self.scheduler.find_job, JOB_NAME)

    def test_submit_errors_are_uncertain_not_job_failures(self) -> None:
        for error in (
            subprocess.CalledProcessError(1, "sbatch", stderr="submit error"),
            subprocess.TimeoutExpired("sbatch", 60), FileNotFoundError("sbatch"),
        ):
            with self.subTest(error=error):
                self.run.side_effect = error
                self.assert_uncertain(self.scheduler.submit, "2016Q1", JOB_NAME)

    def test_wait_pending_running_then_success_with_heartbeat_each_poll(self) -> None:
        self.outputs(queue("PENDING"), queue("RUNNING"), "", accounting())
        heartbeat = Mock()
        self.scheduler.wait("123", heartbeat=heartbeat)
        self.assertEqual(heartbeat.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.sleep.assert_called_with(30.0)
        queue_calls = [call.args[0] for call in self.run.call_args_list if call.args[0][0] == "squeue"]
        self.assertTrue(all("--states=all" in command for command in queue_calls))
        self.assertTrue(all(not any(arg.startswith("--jobs") for arg in command) for command in queue_calls))
        accounting_command = self.run.call_args.args[0]
        self.assertIn("--jobs=123", accounting_command)
        self.assertIn("--starttime=now-30days", accounting_command)

    def test_completed_queue_needs_accounting_zero_exit(self) -> None:
        self.outputs(queue("COMPLETED"), accounting(exit_code="1:0"))
        with self.assertRaisesRegex(JobFailedError, "nonzero exit 1:0"):
            self.scheduler.wait("123")

    def test_wait_accepts_padded_fields_and_cancelled_suffixes(self) -> None:
        self.outputs("", f" 123 | {JOB_NAME} | COMPLETED | 0:0 \n\n")
        self.scheduler.wait("123")
        self.outputs("", accounting("CANCELLED by 1000"))
        with self.assertRaisesRegex(JobFailedError, "CANCELLED"):
            self.scheduler.wait("123")

    def test_wait_rejects_all_terminal_failures_in_queue_and_accounting(self) -> None:
        for state in sorted(gridsat_slurm._FAILED_STATES):
            for source in ("queue", "accounting"):
                with self.subTest(state=state, source=source):
                    outputs = (queue(state),) if source == "queue" else ("", accounting(state))
                    self.outputs(*outputs)
                    with self.assertRaisesRegex(JobFailedError, state):
                        self.scheduler.wait("123")

    def test_wait_rejects_completed_nonzero_exit_or_signal(self) -> None:
        for exit_code in ("2:0", "0:9", "1:15"):
            with self.subTest(exit_code=exit_code):
                self.outputs("", accounting(exit_code=exit_code))
                with self.assertRaisesRegex(JobFailedError, exit_code):
                    self.scheduler.wait("123")

    def test_wait_ignores_steps_and_unrelated_jobs(self) -> None:
        self.outputs(queue(job_id="456"),
                     accounting("FAILED", "1:0", job_id="123.batch") + accounting())
        self.scheduler.wait("123")

    def test_wait_tolerates_accounting_lag_and_missing_exit_code(self) -> None:
        self.outputs("", "", "", accounting("RUNNING"),
                     "", accounting(exit_code=""), "", accounting())
        heartbeat = Mock()
        self.scheduler.wait("123", heartbeat)
        self.assertEqual(heartbeat.call_count, 4)
        self.assertEqual(self.sleep.call_count, 3)

    def test_disappearance_never_means_success_and_polling_is_bounded(self) -> None:
        for stale in ("", accounting("RUNNING"), accounting(exit_code="Unknown"),
                      accounting(job_id="123.batch")):
            with self.subTest(stale=stale):
                self.run.reset_mock()
                self.sleep.reset_mock()
                self.outputs(*(["", stale] * gridsat_slurm._ACCOUNTING_POLLS))
                heartbeat = Mock()
                self.assert_uncertain(self.scheduler.wait, "123", heartbeat)
                self.assertEqual(heartbeat.call_count, 10)
                self.assertEqual(self.run.call_count, 20)
                self.assertEqual(self.sleep.call_count, 9)

    def test_accounting_lag_counter_resets_when_job_reappears(self) -> None:
        self.outputs(*(["", ""] * 9 + [queue()] + ["", ""] * 9 + ["", accounting()]))
        self.scheduler.wait("123")

    def test_wait_rejects_duplicate_or_unknown_results(self) -> None:
        for outputs in (
            (queue() + queue(),), (queue("UNKNOWN_STATE"),),
            ("", accounting() + accounting()), ("", accounting("UNKNOWN_STATE")),
        ):
            with self.subTest(outputs=outputs):
                self.outputs(*outputs)
                self.assert_uncertain(self.scheduler.wait, "123")

    def test_wait_query_failure_is_not_a_disappearance(self) -> None:
        self.run.side_effect = subprocess.CalledProcessError(1, "squeue", stderr="connection error")
        self.assert_uncertain(self.scheduler.wait, "123")
        self.run.assert_called_once()
        self.run.side_effect = [response(""),
                               subprocess.CalledProcessError(1, "sacct", stderr="database error")]
        self.assert_uncertain(self.scheduler.wait, "123")

    def test_wait_has_overall_deadline_without_cancelling_job(self) -> None:
        self.outputs(queue("PENDING"))
        with patch("gdex_downloader.gridsat_slurm.time.monotonic",
                   side_effect=[0, gridsat_slurm._MAX_WAIT_SECONDS]):
            self.assert_uncertain(self.scheduler.wait, "123")
        self.sleep.assert_not_called()
        self.run.assert_called_once()

    def test_heartbeat_failure_propagates_before_query(self) -> None:
        heartbeat = Mock(side_effect=RuntimeError("controller lock lost"))
        with self.assertRaisesRegex(RuntimeError, "controller lock lost"):
            self.scheduler.wait("123", heartbeat)
        self.run.assert_not_called()


@unittest.skipUnless(BASH, "Bash is not available")
class DeploymentScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.app = self.root / "repo with spaces"
        self.app.mkdir()
        self.data = self.root / "data with spaces,commas"
        self.launcher = self.root / "science launcher.sh"
        self.launcher.write_text(
            '#!/bin/bash\n'
            'printf "cwd_name=%s\\n" "${PWD##*/}"\n'
            'printf "argument=<%s>\\n" "$@"\n'
            'printf "proxy=<%s>\\n" "${https_proxy:-}"\n'
            'printf "thread_limit=<%s>\\n" "${OMP_NUM_THREADS:-}"\n'
            'exit "${TEST_EXIT_CODE:-0}"\n', encoding="utf-8", newline="\n",
        )
        self.launcher.chmod(0o755)
        self.env = dict(os.environ, APP_DIR=self.app.as_posix(),
                        DATA_ROOT=self.data.as_posix(), PYTHON_LAUNCHER=self.launcher.as_posix())
        for key in ("BASH_ENV", "ENV", "SHELLOPTS", "EXECUTOR", "WORKERS", "TEST_EXIT_CODE"):
            self.env.pop(key, None)

    def run_script(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(BASH), str(REPO / "deploy" / script), *args], env=self.env,
            cwd=self.root, capture_output=True, text=True, timeout=30,
        )

    def arguments(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cwd_name=repo with spaces", result.stdout)
        return [line[len("argument=<"):-1] for line in result.stdout.splitlines()
                if line.startswith("argument=<")]

    def test_both_scripts_parse_as_bash(self) -> None:
        for script in ("th-hpc6-gridsat.sh", "th-hpc6-gridsat-worker.sbatch"):
            with self.subTest(script=script):
                result = subprocess.run(
                    [str(BASH), "-n", str(REPO / "deploy" / script)],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
        worker = (REPO / "deploy" / "th-hpc6-gridsat-worker.sbatch").read_text()
        self.assertIn("#SBATCH --partition=5090\n", worker)

    def test_controller_default_run_preserves_proxy_and_logs_pid(self) -> None:
        self.env["https_proxy"] = "http://configured.invalid:8080"
        result = self.run_script("th-hpc6-gridsat.sh", "--start-date", "2016-01-01")
        self.assertEqual(self.arguments(result), [
            "-m", "gdex_downloader.gridsat", "run", "--data-root", self.data.as_posix(),
            "--executor", "slurm", "--workers", "2", "--python-launcher", self.launcher.as_posix(),
            "--start-date", "2016-01-01",
        ])
        self.assertIn("proxy=<http://configured.invalid:8080>", result.stdout)
        self.assertRegex(result.stdout, r"controller_pid=[0-9]+")
        logs = list((self.data / "_logs").glob("gridsat-run-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("controller_pid=", logs[0].read_text(encoding="utf-8"))
        self.assertNotIn("--insecure", result.stdout)

    def test_controller_forwards_status_without_run_only_flags(self) -> None:
        result = self.run_script("th-hpc6-gridsat.sh", "status")
        self.assertEqual(self.arguments(result), [
            "-m", "gdex_downloader.gridsat", "status", "--data-root", self.data.as_posix(),
        ])
        self.assertFalse(self.data.exists())
        self.assertNotIn("started_at=", result.stdout)
        self.assertNotIn("controller_pid=", result.stdout)

    def test_controller_dry_run_has_no_root_or_wrapper_output(self) -> None:
        for action in ((), ("run",)):
            with self.subTest(action=action):
                result = self.run_script("th-hpc6-gridsat.sh", *action, "--dry-run")
                arguments = self.arguments(result)
                self.assertEqual(arguments[2], "run")
                self.assertEqual(arguments[-1], "--dry-run")
                self.assertEqual(arguments[arguments.index("--python-launcher") + 1],
                                 self.launcher.as_posix())
                self.assertFalse(self.data.exists())
                self.assertNotIn("started_at=", result.stdout)
                self.assertNotIn("controller_pid=", result.stdout)

    def test_controller_help_has_no_root_or_wrapper_output(self) -> None:
        for flags in (("--help",), ("-h",), ("run", "--help"),
                      ("status", "--help"), ("process", "--help")):
            with self.subTest(flags=flags):
                result = self.run_script("th-hpc6-gridsat.sh", *flags)
                self.assertEqual(self.arguments(result)[-1], flags[-1])
                self.assertFalse(self.data.exists())
                self.assertNotIn("started_at=", result.stdout)
                self.assertNotIn("controller_pid=", result.stdout)

    def test_controller_forwards_process_and_user_overrides(self) -> None:
        result = self.run_script("th-hpc6-gridsat.sh", "process", "--quarter", "2016Q2", "--workers", "1")
        self.assertEqual(self.arguments(result), [
            "-m", "gdex_downloader.gridsat", "process", "--data-root", self.data.as_posix(),
            "--workers", "2", "--quarter", "2016Q2", "--workers", "1",
        ])

    def test_controller_default_root_is_app_sibling_data(self) -> None:
        self.env.pop("DATA_ROOT")
        result = self.run_script("th-hpc6-gridsat.sh", "status")
        arguments = self.arguments(result)
        self.assertTrue(arguments[-1].endswith("/data/gridsat-b1"))
        self.assertFalse((self.app.parent / "data").exists())

    def test_controller_returns_launcher_exit_status(self) -> None:
        self.env["TEST_EXIT_CODE"] = "7"
        result = self.run_script("th-hpc6-gridsat.sh", "status")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)

    def test_worker_positional_settings_override_environment_without_profile(self) -> None:
        self.env.update(APP_DIR="/invalid", DATA_ROOT="/invalid", PYTHON_LAUNCHER="/invalid")
        result = self.run_script("th-hpc6-gridsat-worker.sbatch", self.app.as_posix(),
                                 self.data.as_posix(), self.launcher.as_posix(), "2016Q1", "2")
        self.assertEqual(self.arguments(result), [
            "-m", "gdex_downloader.gridsat", "process", "--data-root", self.data.as_posix(),
            "--quarter", "2016Q1", "--workers", "2",
        ])
        self.assertIn("thread_limit=<1>", result.stdout)
        self.assertNotIn("/etc/profile", (REPO / "deploy" / "th-hpc6-gridsat-worker.sbatch").read_text())

    def test_worker_accepts_explicit_environment_settings(self) -> None:
        self.env.update(QUARTER="2026Q4", WORKERS="1")
        result = self.run_script("th-hpc6-gridsat-worker.sbatch")
        self.assertEqual(self.arguments(result)[-4:], ["--quarter", "2026Q4", "--workers", "1"])

    def test_worker_rejects_invalid_settings(self) -> None:
        for quarter, workers in (("2016Q5", "2"), ("2016Q1", "3")):
            with self.subTest(quarter=quarter, workers=workers):
                self.env.update(QUARTER=quarter, WORKERS=workers)
                result = self.run_script("th-hpc6-gridsat-worker.sbatch")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn("argument=<", result.stdout)


if __name__ == "__main__":
    unittest.main()
