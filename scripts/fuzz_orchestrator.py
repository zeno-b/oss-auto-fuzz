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
from typing import Dict, List, Optional, Set, Tuple

import yaml

EnvMap = Dict[str, str]


@dataclasses.dataclass(slots=True)
class FuzzTarget:
    name: str
    project: str
    fuzz_target: str
    enabled: bool = True
    sanitizer: str = "address"
    dictionary: Optional[Path] = None
    environment: EnvMap = dataclasses.field(default_factory=dict)
    fuzzer_args: List[str] = dataclasses.field(default_factory=list)
    max_run_seconds: int = 900

    @classmethod
    def from_dict(cls, data: Dict) -> "FuzzTarget":
        required = ("name", "project", "fuzz_target")
        missing = [field for field in required if not data.get(field)]
        if missing:
            raise OrchestratorError(
                f"Missing required field(s) {missing} in target definition."
            )

        binaries = data.get("binaries") or []
        default_run_seconds = binaries[0].get("max_run_seconds", 900) if binaries else 900
        default_args_raw = binaries[0].get("args", []) if binaries else []
        default_args = [str(arg) for arg in default_args_raw]
        dictionary = data.get("dictionary")

        dictionary_path = Path(dictionary) if dictionary else None
        environment_raw = data.get("environment", {}) or {}
        if not isinstance(environment_raw, dict):
            raise OrchestratorError(
                f"Environment for target {data.get('name')} must be a mapping."
            )
        environment: EnvMap = {str(key): str(value) for key, value in environment_raw.items()}

        return cls(
            name=data["name"],
            project=data["project"],
            fuzz_target=data["fuzz_target"],
            enabled=data.get("enabled", True),
            sanitizer=data.get("sanitizer", "address"),
            dictionary=dictionary_path,
            environment=environment,
            fuzzer_args=default_args,
            max_run_seconds=int(data.get("max_run_seconds", default_run_seconds)),
        )

    def validate(self) -> None:
        if self.max_run_seconds <= 0:
            raise OrchestratorError(
                f"Target {self.name} must have max_run_seconds > 0 (got {self.max_run_seconds})."
            )
        if self.dictionary and not self.dictionary.exists():
            logging.warning(
                "Dictionary for target %s not found at %s; continuing without it.",
                self.name,
                self.dictionary,
            )
            self.dictionary = None

    def build_args(self) -> List[str]:
        return [
            "build_fuzzers",
            f"--sanitizer={self.sanitizer}",
            self.project,
        ]

    def run_args(self) -> List[str]:
        args = [
            "run_fuzzer",
            f"--sanitizer={self.sanitizer}",
            f"--max_total_time={self.max_run_seconds}",
        ]
        if self.dictionary:
            args.extend(["--dict", str(self.dictionary)])
        args.extend([self.project, self.fuzz_target])
        if self.fuzzer_args:
            args.append("--")
            args.extend(self.fuzzer_args)
        return args


class OrchestratorError(Exception):
    """Raised when orchestration fails."""


def load_targets(config_path: Path) -> List[FuzzTarget]:
    if not config_path.exists():
        raise OrchestratorError(f"Config file not found: {config_path}")

    data = yaml.safe_load(config_path.read_text())
    if not isinstance(data, dict):
        raise OrchestratorError("Config root must be a mapping/object.")

    targets_raw = data.get("targets")
    if not isinstance(targets_raw, list):
        raise OrchestratorError("Config must contain a 'targets' list.")

    parsed: List[FuzzTarget] = []
    errors: List[str] = []
    for index, entry in enumerate(targets_raw, start=1):
        try:
            target = FuzzTarget.from_dict(entry)
            target.validate()
            parsed.append(target)
        except OrchestratorError as exc:
            name = entry.get("name") if isinstance(entry, dict) else "<invalid>"
            errors.append(f"entry #{index} ({name}): {exc}")

    if errors:
        raise OrchestratorError(
            "Invalid target configuration:\n - " + "\n - ".join(errors)
        )

    enabled_targets = [t for t in parsed if t.enabled]
    if not enabled_targets:
        raise OrchestratorError("No enabled targets found in configuration.")

    _ensure_unique_names(enabled_targets)
    return enabled_targets


def _ensure_unique_names(targets: List[FuzzTarget]) -> None:
    seen: Set[str] = set()
    duplicates: Set[str] = set()
    for target in targets:
        if target.name in seen:
            duplicates.add(target.name)
        else:
            seen.add(target.name)
    if duplicates:
        raise OrchestratorError(
            f"Duplicate target names detected: {', '.join(sorted(duplicates))}"
        )


def _merge_env(base: EnvMap, overrides: Optional[EnvMap] = None) -> EnvMap:
    env = base.copy()
    if overrides:
        env.update(overrides)
    return env


def run_helper(helper: Path, args: List[str], env: EnvMap, log_path: Path, label: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["python3", str(helper), *args]
    logging.debug("Executing helper (%s): %s", label, " ".join(cmd))

    process: subprocess.Popen[str] | None = None
    try:
        with log_path.open("a", encoding="utf-8") as logfile:
            logfile.write(f"\n=== Running ({label}): {' '.join(cmd)} ===\n")
            logfile.flush()
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            assert process.stdout
            for line in process.stdout:
                logfile.write(line)
                logfile.flush()
                logging.info("[%s] %s", label, line.rstrip())
            returncode = process.wait()
            if returncode != 0:
                logfile.write(f"\nCommand failed with exit code {returncode}\n")
                logfile.flush()
                raise OrchestratorError(
                    f"Helper command {' '.join(args)} failed (exit {returncode})"
                )
    except OSError as exc:
        raise OrchestratorError(f"Failed to execute helper: {exc}") from exc
    finally:
        if process and process.stdout:
            process.stdout.close()


def build_projects(
    helper: Path,
    targets: List[FuzzTarget],
    artifacts: Path,
    base_env: EnvMap,
) -> None:
    built: Set[Tuple[str, str]] = set()
    for target in targets:
        key = (target.project, target.sanitizer)
        if key in built:
            continue
        log_file = artifacts / target.name / "build.log"
        env = _merge_env(base_env, target.environment)
        logging.info(
            "Building project %s for sanitizer %s (target: %s)",
            target.project,
            target.sanitizer,
            target.name,
        )
        run_helper(helper, target.build_args(), env, log_file, label=f"build:{target.project}:{target.sanitizer}")
        built.add(key)


def run_targets(
    helper: Path,
    targets: List[FuzzTarget],
    artifacts: Path,
    max_parallel: int,
    base_env: EnvMap,
) -> None:
    worker_count = max(1, min(max_parallel, len(targets)))
    logging.info(
        "Running %s enabled target(s) with up to %s parallel worker(s).",
        len(targets),
        worker_count,
    )

    def execute(target: FuzzTarget) -> str:
        log_file = artifacts / target.name / "run.log"
        env = _merge_env(base_env, target.environment)
        env.update(
            {
                "FUZZ_TARGET": target.fuzz_target,
                "FUZZ_PROJECT": target.project,
                "SANITIZER": target.sanitizer,
                "ARTIFACT_DIR": str(artifacts / target.name),
            }
        )

        run_helper(
            helper,
            target.run_args(),
            env,
            log_file,
            label=f"run:{target.name}",
        )
        return target.name

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {executor.submit(execute, target): target for target in targets}
        for future in as_completed(future_map):
            target = future_map[future]
            try:
                future.result()
                logging.info("Target %s completed successfully.", target.name)
            except Exception as exc:  # noqa: BLE001
                for pending in future_map:
                    pending.cancel()
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
        base_env = os.environ.copy()
        build_projects(helper_path, targets, artifacts_dir, base_env)
        run_targets(helper_path, targets, artifacts_dir, args.max_parallel, base_env)
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
