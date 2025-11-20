# OSS Auto Fuzz

This repository ships a self-contained deployment workflow that bootstraps the
[google/oss-fuzz](https://github.com/google/oss-fuzz) toolchain inside Docker
Compose for remote code execution (RCE) exploit discovery across multiple
binaries. A single Python script orchestrates prerequisite installation,
repository cloning, container builds, stack bring-up, and full rollback.

## Repository Layout

- `scripts/deploy.py` – main entrypoint. Handles prerequisite checks, cloning
  `oss-fuzz`, building the Docker images, starting/stopping Compose services,
  and storing deployment metadata for status/rollback.
- `scripts/fuzz_orchestrator.py` – runs inside the runner container. It reads
  `config/fuzz_targets.yaml`, builds enabled projects once, then executes the
  configured fuzzers (optionally in parallel) while storing logs in `artifacts/`.
- `config/fuzz_targets.yaml` – declarative list of targets, sanitizers, runtime
  limits, dictionaries, and custom environment variables.
- `docker/oss_fuzz_env.Dockerfile` – hardened multi-stage image derived from the
  OSS-Fuzz builder base with additional RCE tooling (radare2, binwalk, gdb, etc).
- `docker-compose.yml` – builder + runner stack definition with shared volumes
  for configuration and artifacts.

## Requirements

- Linux host with sudo access.
- Python 3.10+.
- Docker Engine and Docker Compose plugin (`deploy.py --auto-install` can invoke
  `apt-get` if they are missing).

## Quick Start

```bash
# Inspect commands and flags
python3 scripts/deploy.py --help

# Deploy stack (installs missing deps, clones repo, builds, launches)
python3 scripts/deploy.py deploy --auto-install

# Check container/state summary
python3 scripts/deploy.py status

# Roll everything back and delete artifacts
python3 scripts/deploy.py rollback
```

On deploy the script:

1. Verifies `git`, `docker`, and `docker compose`, optionally installing them.
2. Clones or updates `google/oss-fuzz` under `./oss-fuzz`.
3. Builds `docker/oss_fuzz_env.Dockerfile` and the compose services.
4. Starts the builder and runner containers. The runner automatically loads
   `config/fuzz_targets.yaml`, builds enabled projects, and executes each fuzzer
   while writing logs/crashes to `./artifacts/<target>/`.

## Configuring Targets

Edit `config/fuzz_targets.yaml` to toggle or add fuzzers. Supported fields:

- `name` – friendly label used for artifact directories.
- `project` – OSS-Fuzz project slug.
- `fuzz_target` – fuzzer entry point for `infra/helper.py run_fuzzer`.
- `sanitizer` – `address`, `undefined`, `memory`, etc.
- `dictionary` – optional path to a dictionary inside the shared workspace.
- `environment` – extra env vars (e.g., `UBSAN_OPTIONS`).
- `binaries[*].args` – extra CLI args passed to the fuzzer (`--` separator).
- `binaries[*].max_run_seconds` – becomes `--max_total_time`.
- `enabled` – set to `false` to skip a target without removing it.

Artifacts live under `artifacts/<name>/` and include `build.log` and `run.log`
for quick triage.

## Rollback & Cleanup

Run `python3 scripts/deploy.py rollback` to:

- Stop the compose stack (`docker compose down -v --remove-orphans`).
- Remove the cloned `oss-fuzz` repo, artifact/log directories, and cached state.

Deployments automatically trigger rollback on failure unless
`--no-rollback` is provided.

## Observability

The orchestrator streams every build/run log line to both stdout and the
corresponding artifact directory, making it simple to ship logs to an external
stack or inspect them locally. Add more targets, sanitizers, or auxiliary tools
by editing the YAML file and re-running the deploy workflow.