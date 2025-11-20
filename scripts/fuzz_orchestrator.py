#!/usr/bin/env python3
"""
Lightweight orchestrator that builds and runs OSS-Fuzz targets inside the container.

The script reads config/fuzz_targets.yaml, builds enabled projects once, and
then runs each target (optionally in parallel) while streaming logs to both
stdout and target-specific artifact directories for later triage.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional

import yaml


@dataclasses.dataclass
class FuzzTarget:
    name: str
    project: str
    fuzz_target: str
    enabled: bool = True
    sanitizer: str = "address"
    dictionary: Optional[str] = None
    environment: Dict[str, str] = dataclasses.field(default_factory=dict)
    fuzzer_args: List[str] = dataclasses.field(default_factory=list)
    max_run_seconds: int = 900

    @classmethod
    def from_dict(cls, data: Dict) -> "FuzzTarget":
        binaries = data.get("binaries") or []
        default_run_seconds = binaries[0].get("max_run_seconds", 900) if binaries else 900
        default_args = binaries[0].get("args", []) if binaries else []
        return cls(
            name=data["name"],
            project=data["project"],
            fuzz_target=data["fuzz_target"],
            enabled=data.get("enabled", True),
            sanitizer=data.get("sanitizer", "address"),
            dictionary=data.get("dictionary"),
            environment=data.get("environment", {}) or {},
            fuzzer_args=default_args,
            max_run_seconds=data.get("max_run_seconds", default_run_seconds),
        )


class OrchestratorError(Exception):
    """Raised when orchestration fails."""


def load_targets(config_path: Path) -> List[FuzzTarget]:
    if not config_path.exists():
        raise OrchestratorError(f"Config file not found: {config_path}")
    data = yaml.safe_load(config_path.read_text())
    targets_raw = data.get("targets", [])
    targets = [FuzzTarget.from_dict(entry) for entry in targets_raw]
    enabled_targets = [t for t in targets if t.enabled]
    if not enabled_targets:
        raise OrchestratorError("No enabled targets found in configuration.")
    return enabled_targets


def run_helper(helper: Path, args: List[str], env: Dict[str, str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["python3", str(helper), *args]
    logging.debug("Executing helper command: %s", " ".join(cmd))
    with log_path.open("a", encoding="utf-8") as logfile:
        logfile.write(f"\n=== Running: {' '.join(cmd)} ===\n")
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        assert process.stdout
        for line in process.stdout:
            logfile.write(line)
            logging.info("[%s] %s", log_path.parent.name, line.rstrip())
        process.wait()
        if process.returncode != 0:
            logfile.write(f"\nCommand failed with exit code {process.returncode}\n")
            raise OrchestratorError(
                f"Helper command {' '.join(args)} failed (exit {process.returncode})"
            )


def build_projects(helper: Path, targets: List[FuzzTarget], artifacts: Path) -> None:
    built = set()
    for target in targets:
        if target.project in built:
            continue
        log_file = artifacts / target.name / "build.log"
        env = os.environ.copy()
        env.update(target.environment)
        args = [
            "build_fuzzers",
            f"--sanitizer={target.sanitizer}",
            target.project,
        ]
        run_helper(helper, args, env, log_file)
        built.add(target.project)


def run_targets(
    helper: Path,
    targets: List[FuzzTarget],
    artifacts: Path,
    max_parallel: int,
) -> None:
    def execute(target: FuzzTarget) -> str:
        log_file = artifacts / target.name / "run.log"
        env = os.environ.copy()
        env.update(
            {
                "FUZZ_TARGET": target.fuzz_target,
                "FUZZ_PROJECT": target.project,
                "SANITIZER": target.sanitizer,
                "ARTIFACT_DIR": str(artifacts / target.name),
            }
        )
        env.update(target.environment)

        args = [
            "run_fuzzer",
            f"--sanitizer={target.sanitizer}",
            f"--max_total_time={target.max_run_seconds}",
        ]
        if target.dictionary:
            args.extend(["--dict", target.dictionary])
        args.extend([target.project, target.fuzz_target])
        if target.fuzzer_args:
            args.append("--")
            args.extend(target.fuzzer_args)

        run_helper(helper, args, env, log_file)
        return target.name

    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        future_map = {executor.submit(execute, target): target for target in targets}
        for future in as_completed(future_map):
            target = future_map[future]
            try:
                future.result()
                logging.info("Target %s completed successfully.", target.name)
            except Exception as exc:  # noqa: BLE001
                raise OrchestratorError(f"Target {target.name} failed: {exc}") from exc


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/workspace/config/fuzz_targets.yaml")
    parser.add_argument("--artifacts", default="/workspace/artifacts")
    parser.add_argument("--oss-fuzz", default="/workspace/oss-fuzz")
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=max(1, os.cpu_count() // 4 if os.cpu_count() else 1),
        help="Maximum number of fuzzers to run in parallel.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("ORCHESTRATOR_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    config_path = Path(args.config)
    artifacts_dir = Path(args.artifacts)
    oss_fuzz_dir = Path(args.oss_fuzz)
    helper_path = oss_fuzz_dir / "infra" / "helper.py"

    if not helper_path.exists():
        logging.error("helper.py not found at %s", helper_path)
        return 1

    try:
        targets = load_targets(config_path)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        build_projects(helper_path, targets, artifacts_dir)
        run_targets(helper_path, targets, artifacts_dir, args.max_parallel)
    except OrchestratorError as exc:
        logging.error("Orchestration failed: %s", exc)
        return 1
    except yaml.YAMLError as exc:
        logging.error("Failed to parse YAML config: %s", exc)
        return 1
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
