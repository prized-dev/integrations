"""SWE-bench evaluation in Prized sandboxes, without Docker.

    python -m minisweagent_prized.evaluate --predictions_path preds.json --max_workers 32 --run_id my-run
    python -m minisweagent_prized.evaluate --predictions_path gold --run_id gold   # the reference patches

Each instance runs the way SWE-bench's own harness runs it
(``swebench.harness.run_evaluation.run_instance``), in a Prized sandbox instead
of a Docker container: the instance's image, the prediction applied with the
same ``git apply``/``patch`` fallbacks, the instance's eval script under the
same timeout, graded by swebench's ``get_eval_report``. Logs land where
swebench puts them (``logs/run_evaluation/<run_id>/<model>/<instance_id>/``),
and the final report is swebench's own ``make_run_report``. Each instance's
directory also gets ``prized.json``: its sandbox, timings and attempts.

Needs ``swebench`` 5 or later (``pip install "mini-swe-agent-prized[swebench]"``),
whose datasets carry each instance's image and eval script, and
``PRIZED_API_KEY``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from prized import CommandTimeoutError, PrizedError, Sandbox
from swebench.harness.constants import (
    APPLY_PATCH_FAIL,
    APPLY_PATCH_PASS,
    CONTAINER_PATCH_FILE,
    LOG_INSTANCE,
    LOG_REPORT,
    LOG_TEST_OUTPUT,
    RUN_EVALUATION_LOG_DIR,
)
from swebench.harness.grading import get_eval_report
from swebench.harness.reporting import make_run_report
from swebench.harness.run_evaluation import GIT_APPLY_CMDS, write_run_metadata
from swebench.harness.utils import EvaluationError, get_predictions_from_file, load_swebench_dataset, make_test_spec
from swebench.image_builder.constants import CONTAINER_WORKDIR
from swebench.logger import close_logger, setup_logger

from .environment import RUNTIME_FAILED, PrizedEnvironment, command_script

PRIZED_LOG = "prized.json"
# The workspace is full or busy: wait for a slot instead of spending a retry.
WAIT_CODES = ("sandbox_quota_exceeded", "rate_limited")
_LABEL_VALUE = re.compile(r"[^A-Za-z0-9._-]")
# Startup (pull and unpack) plus the eval timeout plus slack: past this a sandbox deletes itself.
SANDBOX_SLACK_MIN = 10


class InfraError(Exception):
    """Prized, not the prediction, failed after the sandbox started: nothing was graded."""


def label_value(text: str) -> str:
    """A label value for the run: safe characters, and a hash so two run ids never share one."""
    digest = hashlib.sha1(text.encode()).hexdigest()[:8]
    return f"{_LABEL_VALUE.sub('-', text)[:54]}-{digest}"


def image_for(test_spec, image_template: str | None) -> str:
    """The instance's image, or its name in a mirror (``{instance_id}`` and ``{image}`` are filled in)."""
    if not image_template:
        return test_spec.image
    return image_template.format(instance_id=test_spec.instance_id, image=test_spec.image)


def _exec(env: PrizedEnvironment, command: str, cwd: str, timeout: float) -> tuple[str, int, bool]:
    """Output (stdout and stderr together), exit code and whether it timed out, of one command run
    as ``docker exec`` runs it: bash -c in ``cwd``, with the image's environment."""
    script = command_script(command, cwd, env._env(), ["/bin/bash", "-c"])
    try:
        result = env.run_script(script, timeout)
    except CommandTimeoutError as exc:
        return (exc.result.stdout or "") + (exc.result.stderr or ""), -1, True
    except PrizedError as exc:
        raise InfraError(f"{command[:60]}: {exc}") from exc
    if result.exit_code == RUNTIME_FAILED and (result.stderr or "").startswith("prized runtime:"):
        # runtime.py could not enter the image: the command never ran.
        raise InfraError(f"{command[:60]}: {result.stderr.strip()[:300]}")
    return result.stdout + result.stderr, result.exit_code, False


def run_instance(
    instance: dict,
    pred: dict,
    run_id: str,
    timeout: int,
    *,
    env_options: dict[str, Any] | None = None,
    image_template: str | None = None,
    log_root: Path = RUN_EVALUATION_LOG_DIR,
) -> dict[str, Any]:
    """Evaluates one prediction. Returns what happened: ``status`` is ``resolved``, ``unresolved``,
    ``error`` (the harness's own error, as swebench counts it) or ``infra`` (Prized failed: no
    sandbox or image, or a command that never ran; nothing was graded, worth a retry); ``cached``
    if a report was already there."""
    test_spec = make_test_spec(instance)
    instance_id = test_spec.instance_id
    model = pred.get("model_name_or_path", "None").replace("/", "__")
    log_dir = log_root / run_id / model / instance_id
    report_path = log_dir / LOG_REPORT
    if report_path.exists():
        report = json.loads(report_path.read_text())
        return {"instance_id": instance_id, "status": "cached", "resolved": report[instance_id]["resolved"]}
    if test_spec.image_assets:
        msg = "needs image assets (SWE-bench Multimodal), which this runner doesn't stage"
        return {"instance_id": instance_id, "status": "error", "error": msg}

    log_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(instance_id, log_dir / LOG_INSTANCE)
    info: dict[str, Any] = {
        "instance_id": instance_id,
        "status": "error",
        "image": image_for(test_spec, image_template),
    }
    started = time.monotonic()
    env = None
    try:
        try:
            env = PrizedEnvironment(
                image=info["image"],
                cwd=CONTAINER_WORKDIR,
                logger=logger,
                **{
                    "labels": {"swebench-run": label_value(run_id)},
                    "max_runtime_min": (900 + timeout) // 60 + SANDBOX_SLACK_MIN,
                    **(env_options or {}),
                },
            )
        except Exception as exc:  # noqa: BLE001 - nothing was evaluated: reported, and retried by run()
            info.update(status="infra", error=f"{type(exc).__name__}: {exc}", error_code=getattr(exc, "code", None))
            logger.error(f"Could not start a Prized sandbox with {info['image']}: {exc}")
            return info
        info["sandbox_id"] = env.sandbox.id if env.sandbox else None
        info["timings"] = dict(env.timings)
        logger.info(f"Container for {instance_id} started: Prized sandbox {info['sandbox_id']}")

        patch_file = log_dir / "patch.diff"
        patch_file.write_text(pred["model_patch"] or "")
        logger.info(f"Intermediate patch for {instance_id} written to {patch_file}, now applying to container...")
        env.write_file(CONTAINER_PATCH_FILE, patch_file.read_bytes())

        applied, output = False, ""
        for attempt, git_apply_cmd in enumerate(GIT_APPLY_CMDS):
            if attempt:
                # a failed attempt (notably --reject) leaves partial state behind; start from a pristine tree
                _exec(env, "git checkout -- . ; git clean -fd", CONTAINER_WORKDIR, 600)
            output, code, _ = _exec(env, f"{git_apply_cmd} {CONTAINER_PATCH_FILE}", CONTAINER_WORKDIR, 600)
            if code == 0:
                logger.info(f"{APPLY_PATCH_PASS}:\n{output}")
                applied = True
                break
            logger.info(f"Failed to apply patch to container: {git_apply_cmd}")
        if not applied:
            _, code, _ = _exec(env, f"git apply --check --reverse {CONTAINER_PATCH_FILE}", CONTAINER_WORKDIR, 600)
            if code == 0:
                logger.info(f"{APPLY_PATCH_PASS}: verified already applied")
                applied = True
        if not applied:
            logger.info(f"{APPLY_PATCH_FAIL}:\n{output}")
            raise EvaluationError(instance_id, f"{APPLY_PATCH_FAIL}:\n{output}", logger)

        diff_before = _exec(env, "git -c core.fileMode=false diff", CONTAINER_WORKDIR, 600)[0].strip()
        logger.info(f"Git diff before:\n{diff_before}")

        eval_file = log_dir / "eval.sh"
        eval_file.write_text(test_spec.eval_script)
        logger.info(f"Eval script for {instance_id} written to {eval_file}; copying to container...")
        env.write_file("/eval.sh", test_spec.eval_script)

        eval_started = time.monotonic()
        test_output, _, timed_out = _exec(env, "/bin/bash /eval.sh", env.image_workdir, timeout)
        info["timings"]["eval_s"] = total_runtime = time.monotonic() - eval_started
        test_output_path = log_dir / LOG_TEST_OUTPUT
        logger.info(f"Test runtime: {total_runtime:_.2f} seconds")
        with open(test_output_path, "w") as f:
            f.write(test_output)
            logger.info(f"Test output for {instance_id} written to {test_output_path}")
            if timed_out:
                f.write(f"\n\nTimeout error: {timeout} seconds exceeded.")
                raise EvaluationError(instance_id, f"Test timed out after {timeout} seconds.", logger)

        diff_after = _exec(env, "git -c core.fileMode=false diff", CONTAINER_WORKDIR, 600)[0].strip()
        logger.info(f"Git diff after:\n{diff_after}")
        if diff_after != diff_before:
            logger.info("Git diff changed after running eval script")

        logger.info(f"Grading answer for {instance_id}...")
        report = get_eval_report(
            test_spec=test_spec, prediction=pred, test_log_path=test_output_path, include_tests_status=True
        )
        resolved = report[instance_id]["resolved"]
        logger.info(f"report: {report}\nResult for {instance_id}: resolved: {resolved}")
        report_path.write_text(json.dumps(report, indent=4))
        info.update(status="resolved" if resolved else "unresolved", resolved=resolved)
        return info
    except InfraError as exc:
        info.update(status="infra", error=f"InfraError: {exc}"[:500])
        logger.error(f"Prized failed while evaluating {instance_id} (not graded): {exc}")
        return info
    except EvaluationError as exc:
        info["error"] = str(exc).splitlines()[0][:500] if str(exc) else type(exc).__name__
        logger.info(traceback.format_exc())
        return info
    except Exception as exc:  # noqa: BLE001 - as swebench's run_instance: logged, counted as an error
        info["error"] = f"{type(exc).__name__}: {exc}"[:500]
        logger.error(f"Error in evaluating model for {instance_id}: {exc}\n{traceback.format_exc()}")
        return info
    finally:
        if env is not None:
            env.cleanup()
        info["wall_s"] = time.monotonic() - started
        close_logger(logger)


def run(
    instances: list[dict],
    predictions: dict[str, dict],
    run_id: str,
    *,
    max_workers: int = 8,
    timeout: int = 1800,
    retries: int = 1,
    env_options: dict[str, Any] | None = None,
    image_template: str | None = None,
    should_start: Callable[[], bool] | None = None,
    on_result: Callable[[dict], None] | None = None,
    quota_wait_s: float = 1800,
) -> list[dict]:
    """Evaluates ``instances`` that have a non-empty prediction, ``max_workers`` at a time.

    An instance whose sandbox or image didn't come up is tried again, up to ``retries`` more times;
    one whose patch or tests failed is not. A sandbox refused because the workspace is at a quota
    (sandboxes, vCPU, memory or disk) is asked for again, for up to ``quota_wait_s`` seconds,
    without spending a retry. ``should_start`` is asked before each attempt (a spend cap, say):
    when it says no, the instance is left out (status ``skipped``)."""
    todo = [i for i in instances if predictions.get(i["instance_id"], {}).get("model_patch")]
    lock, results = threading.Lock(), []

    def one(instance: dict) -> dict:
        pred = predictions[instance["instance_id"]]
        attempts: list[dict] = []
        tries, waited, pause = 0, 0.0, 5.0
        while True:
            if should_start is not None and not should_start():
                info = {"instance_id": instance["instance_id"], "status": "skipped"}
                break
            info = run_instance(instance, pred, run_id, timeout, env_options=env_options, image_template=image_template)
            if info["status"] == "infra" and info.get("error_code") in WAIT_CODES and waited < quota_wait_s:
                time.sleep(pause)
                waited, pause = waited + pause, min(60.0, pause * 1.5)
                continue
            attempts.append({k: info.get(k) for k in ("status", "error", "sandbox_id", "wall_s")})
            tries += 1
            if info["status"] != "infra" or tries > max(0, retries):
                break
        if waited:
            info["quota_wait_s"] = round(waited)
        info["attempts"] = attempts
        model = pred.get("model_name_or_path", "None").replace("/", "__")
        log_dir = RUN_EVALUATION_LOG_DIR / run_id / model / instance["instance_id"]
        if info["status"] != "cached" and log_dir.exists():
            (log_dir / PRIZED_LOG).write_text(json.dumps(info, indent=2, default=str))
        with lock:
            results.append(info)
            if on_result is not None:
                on_result(info)
        return info

    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = [pool.submit(one, instance) for instance in todo]
        for future in as_completed(futures):
            future.result()
    except BaseException:
        # Interrupted: drop what hasn't started; running instances clean up their sandboxes.
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return results


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dataset_name", default="SWE-bench/SWE-bench_Verified")
    p.add_argument("--split", default="test")
    p.add_argument("--instance_ids", nargs="+", default=None)
    p.add_argument("--predictions_path", required=True, help='a .json/.jsonl of predictions, or "gold"')
    p.add_argument("--max_workers", type=int, default=8, help="sandboxes at a time")
    p.add_argument("--timeout", type=int, default=1800, help="seconds for each instance's eval script")
    p.add_argument("--run_id", required=True)
    p.add_argument("--report_dir", default=".")
    p.add_argument("--retries", type=int, default=1, help="more tries for an instance whose sandbox or image failed")
    p.add_argument("--vcpu", type=int, default=2)
    p.add_argument("--memory_mb", type=int, default=4096)
    p.add_argument(
        "--disk_gb", type=int, default=16, help="the unpacked image and the tests' files (images take 2-6 GB)"
    )
    p.add_argument("--template", default="prized-base-v1")
    p.add_argument("--label", action="append", default=[], help="k=v, added to every sandbox")
    p.add_argument(
        "--image_template",
        default=None,
        help="pull from a mirror, e.g. ghcr.io/epoch-research/swe-bench.eval.x86_64.{instance_id}:latest",
    )
    p.add_argument("--max_sandbox_hours", type=float, default=None, help="stop starting instances past this")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    predictions_list = get_predictions_from_file(args.predictions_path, args.dataset_name, args.split)
    predictions = {p["instance_id"]: p for p in predictions_list}
    if args.instance_ids:
        predictions = {k: v for k, v in predictions.items() if k in set(args.instance_ids)}
    dataset = load_swebench_dataset(args.dataset_name, args.split, args.instance_ids)
    instances = [i for i in dataset if i["instance_id"] in predictions]
    write_run_metadata(args.run_id, args.dataset_name, args.split, None)
    labels = dict(kv.split("=", 1) for kv in args.label if "=" in kv)
    env_options = {
        "vcpu": args.vcpu,
        "memory_mb": args.memory_mb,
        "disk_gb": args.disk_gb,
        "template": args.template,
        "labels": {"swebench-run": label_value(args.run_id), **labels},
    }
    used, used_lock = [0.0], threading.Lock()

    def account(info: dict) -> None:
        with used_lock:
            used[0] += sum((a.get("wall_s") or 0) for a in info.get("attempts", [])) / 3600
        print(f"{info['instance_id']}: {info['status']}" + (f" ({info.get('error')})" if info.get("error") else ""))

    def should_start() -> bool:
        return args.max_sandbox_hours is None or used[0] < args.max_sandbox_hours

    print(f"Evaluating {len(instances)} instances in Prized sandboxes, {args.max_workers} at a time")
    try:
        run(
            instances,
            predictions,
            args.run_id,
            max_workers=args.max_workers,
            timeout=args.timeout,
            retries=args.retries,
            env_options=env_options,
            image_template=args.image_template,
            should_start=should_start,
            on_result=account,
        )
    finally:
        # Anything a crash or an interrupt left behind.
        Sandbox.delete_by_labels({"swebench-run": label_value(args.run_id)})
    make_run_report(predictions, dataset, args.run_id, None, args.report_dir)


if __name__ == "__main__":
    main()
