#!/usr/bin/env python3
"""
Deploy and manage a self-contained OSS-Fuzz + RCE triage environment.

The script bootstraps prerequisites, clones the upstream oss-fuzz repo,
builds the local Docker images, and brings the docker-compose stack online.
It also exposes a rollback routine that tears everything down and wipes
artifacts to ensure a clean state after failures.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, List

REPO_URL = "https://github.com/google/oss-fuzz.git"
ROOT_DIR = Path(__file__).resolve().parent.parent
OSS_FUZZ_DIR = ROOT_DIR / "oss-fuzz"
ARTIFACT_DIR = ROOT_DIR / "artifacts"
LOG_DIR = ROOT_DIR / "logs"
CONFIG_DIR = ROOT_DIR / "config"
COMPOSE_FILE = ROOT_DIR / "docker-compose.yml"
STATE_FILE = ROOT_DIR / ".deploy_state.json"


class DeployError(Exception):
    """Raised when deployment operations fail."""


def run_command(
    command: List[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess:
    """
    Run a shell command with logging and error translation.

    Args:
        command: The argv list to execute.
        cwd: Optional working directory.
        check: Raise DeployError when exit status is non-zero.
        capture_output: When True, capture stdout/stderr.
    """
    logging.debug("Running command: %s", " ".join(command))
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=capture_output,
            text=True,
        )
    except FileNotFoundError as exc:
        raise DeployError(f"Command not found: {command[0]}") from exc

    if check and result.returncode != 0:
        output = result.stdout.strip() if result.stdout else ""
        error = result.stderr.strip() if result.stderr else ""
        raise DeployError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"STDOUT: {output}\nSTDERR: {error}"
        )
    return result


def _command_exists(cmd: List[str]) -> bool:
    try:
        run_command(cmd, capture_output=True)
        return True
    except DeployError:
        return False


class DeployManager:
    def __init__(
        self,
        *,
        auto_install: bool = False,
        skip_build: bool = False,
        rollback_on_failure: bool = True,
        force_reclone: bool = False,
    ) -> None:
        self.auto_install = auto_install
        self.skip_build = skip_build
        self.rollback_on_failure = rollback_on_failure
        self.force_reclone = force_reclone

    def deploy(self) -> None:
        logging.info("Starting OSS-Fuzz environment deployment...")
        try:
            self._ensure_prerequisites()
            self._prepare_directories()
            self._sync_oss_fuzz_repo()
            self._ensure_config()
            if not self.skip_build:
                self._build_images()
            self._compose_up()
            self._write_state()
            logging.info("Deployment completed successfully.")
        except Exception as exc:  # noqa: BLE001
            logging.error("Deployment failed: %s", exc)
            if self.rollback_on_failure:
                logging.info("Rolling back partial deployment...")
                self.rollback()
            raise

    def rollback(self) -> None:
        logging.info("Tearing down environment and cleaning artifacts...")
        if COMPOSE_FILE.exists():
            try:
                self._docker_compose_cmd("down", "-v", "--remove-orphans")
            except DeployError as exc:
                logging.warning("docker compose down failed: %s", exc)
        for path in (OSS_FUZZ_DIR, ARTIFACT_DIR, LOG_DIR):
            if path.exists():
                logging.debug("Removing %s", path)
                shutil.rmtree(path, ignore_errors=True)
        if STATE_FILE.exists():
            STATE_FILE.unlink(missing_ok=True)
        logging.info("Rollback completed.")

    def status(self) -> None:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            logging.info("Last deployment: %s", json.dumps(data, indent=2))
        else:
            logging.info("No previous deployment state found.")
        try:
            self._docker_compose_cmd("ps")
        except DeployError as exc:
            logging.warning("Unable to query docker compose state: %s", exc)

    # Internal helpers -----------------------------------------------------

    def _ensure_prerequisites(self) -> None:
        logging.info("Verifying prerequisites...")

        def verify(name: str, cmd: List[str]) -> tuple[str, bool]:
            return name, _command_exists(cmd)

        checks = {
            "git": ["git", "--version"],
            "docker": ["docker", "--version"],
            "docker compose": ["docker", "compose", "version"],
        }

        missing: List[str] = []
        with ThreadPoolExecutor(max_workers=len(checks)) as executor:
            futures = [executor.submit(verify, name, cmd) for name, cmd in checks.items()]
            for future in as_completed(futures):
                name, ok = future.result()
                if ok:
                    logging.debug("Found prerequisite: %s", name)
                else:
                    logging.warning("Missing prerequisite: %s", name)
                    missing.append(name)

        if missing:
            if not self.auto_install:
                raise DeployError(
                    f"Missing prerequisites {missing}. "
                    "Re-run with --auto-install to install via apt."
                )
            self._install_prerequisites(missing)

        self._verify_docker_daemon()

    def _install_prerequisites(self, missing: Iterable[str]) -> None:
        if shutil.which("apt-get") is None:
            raise DeployError("Auto-install requested but apt-get is unavailable.")

        package_map = {
            "git": "git",
            "docker": "docker.io",
            "docker compose": "docker-compose-plugin",
        }
        apt_packages = sorted({package_map[name] for name in missing if name in package_map})
        if not apt_packages:
            return
        logging.info("Installing packages via apt: %s", ", ".join(apt_packages))
        run_command(["sudo", "apt-get", "update"])
        run_command(
            ["sudo", "apt-get", "install", "-y", "--no-install-recommends", *apt_packages]
        )

    def _verify_docker_daemon(self) -> None:
        try:
            run_command(["docker", "info"], capture_output=True)
        except DeployError as exc:
            raise DeployError(
                "Docker daemon is unreachable. Ensure it is running and you have permission."
            ) from exc

    def _prepare_directories(self) -> None:
        for path in (ARTIFACT_DIR, LOG_DIR, CONFIG_DIR):
            path.mkdir(parents=True, exist_ok=True)

    def _sync_oss_fuzz_repo(self) -> None:
        if self.force_reclone and OSS_FUZZ_DIR.exists():
            logging.info("Force reclone requested; deleting existing oss-fuzz checkout.")
            shutil.rmtree(OSS_FUZZ_DIR, ignore_errors=True)

        if OSS_FUZZ_DIR.exists():
            logging.info("Updating existing oss-fuzz checkout...")
            run_command(["git", "fetch", "--all"], cwd=OSS_FUZZ_DIR)
            run_command(["git", "reset", "--hard", "origin/master"], cwd=OSS_FUZZ_DIR)
        else:
            logging.info("Cloning oss-fuzz repository...")
            run_command(["git", "clone", "--depth", "1", REPO_URL, str(OSS_FUZZ_DIR)])

    def _ensure_config(self) -> None:
        config_file = CONFIG_DIR / "fuzz_targets.yaml"
        if not config_file.exists():
            raise DeployError(
                f"Expected config file at {config_file}. "
                "Please create it or copy the provided example."
            )

    def _build_images(self) -> None:
        logging.info("Building docker images (this may take a while)...")
        self._docker_compose_cmd("build", "--pull")

    def _compose_up(self) -> None:
        logging.info("Starting docker compose stack...")
        self._docker_compose_cmd("up", "-d", "--remove-orphans")

    def _write_state(self) -> None:
        commit = None
        if OSS_FUZZ_DIR.exists():
            result = run_command(["git", "rev-parse", "HEAD"], cwd=OSS_FUZZ_DIR, capture_output=True)
            commit = result.stdout.strip()

        state = {
            "timestamp": int(time.time()),
            "oss_fuzz_commit": commit,
            "compose_file": str(COMPOSE_FILE),
        }
        STATE_FILE.write_text(json.dumps(state, indent=2))

    def _docker_compose_cmd(self, *args: str) -> None:
        if not COMPOSE_FILE.exists():
            raise DeployError(f"docker compose file is missing at {COMPOSE_FILE}")
        run_command(["docker", "compose", "-f", str(COMPOSE_FILE), *args])


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    deploy_parser = subparsers.add_parser("deploy", help="Deploy the OSS-Fuzz stack")
    deploy_parser.add_argument(
        "--auto-install",
        action="store_true",
        help="Automatically install missing prerequisites via apt.",
    )
    deploy_parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip docker image builds and only run docker compose up.",
    )
    deploy_parser.add_argument(
        "--no-rollback",
        action="store_true",
        help="Do not rollback on deployment failure.",
    )
    deploy_parser.add_argument(
        "--force-reclone",
        action="store_true",
        help="Force delete and reclone the oss-fuzz repository.",
    )

    subparsers.add_parser("rollback", help="Rollback and clean all resources")
    subparsers.add_parser("status", help="Display deployment status")

    parser.add_argument(
        "--log-level",
        default=os.environ.get("DEPLOY_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Set logging verbosity.",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    manager = DeployManager(
        auto_install=getattr(args, "auto_install", False),
        skip_build=getattr(args, "skip_build", False),
        rollback_on_failure=not getattr(args, "no_rollback", False),
        force_reclone=getattr(args, "force_reclone", False),
    )

    try:
        if args.command == "deploy":
            manager.deploy()
        elif args.command == "rollback":
            manager.rollback()
        elif args.command == "status":
            manager.status()
        else:
            raise DeployError(f"Unknown command {args.command}")
    except DeployError as exc:
        logging.error("Error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
