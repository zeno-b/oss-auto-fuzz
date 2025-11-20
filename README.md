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

## Example Use Cases

1. **Baseline libpng fuzzing**  
   `python3 scripts/deploy.py deploy && tail -f artifacts/libpng_decoder/run.log`

2. **Switch sanitizer for a single target**  
   Edit the target entry to `sanitizer: undefined`, then run  
   `docker compose exec oss-fuzz-runner python3 /workspace/scripts/fuzz_orchestrator.py --config /workspace/config/fuzz_targets.yaml`

3. **Rapid rebuild after patching OSS-Fuzz project files**  
   Apply patches inside `oss-fuzz/projects/<project>` and run  
   `docker compose run --rm oss-fuzz-builder bash -lc "./infra/helper.py build_fuzzers <project>"`

4. **Parallel fuzzing blitz across CPU cores**  
   `docker compose exec oss-fuzz-runner python3 /workspace/scripts/fuzz_orchestrator.py --max-parallel $(nproc --ignore=2)`

5. **Target-specific dictionary tuning**  
   Drop a dictionary under `config/dicts/target.dict`, reference it in the YAML, redeploy with  
   `python3 scripts/deploy.py deploy --skip-build`

6. **CI smoke test without long fuzz runs**  
   Update `binaries[*].max_run_seconds` to a small value (e.g., 60) and run  
   `python3 scripts/deploy.py deploy --skip-build`

7. **Nightly cleanup and redeploy**  
   `python3 scripts/deploy.py rollback && python3 scripts/deploy.py deploy`

8. **Collecting crashing inputs for triage**  
   After a run, inspect `artifacts/<target>/crashes/` and replay inside the runner with  
   `docker compose exec oss-fuzz-runner bash -lc "python3 infra/helper.py reproduce <project> <fuzz_target> artifacts/<target>/crashes/id:000000"`

9. **Expanding coverage to a new project**  
   Add a new entry in `config/fuzz_targets.yaml`, then run  
   `python3 scripts/deploy.py deploy --skip-build` to pick up the config.

10. **Full environment health check**  
    `python3 scripts/deploy.py status && docker compose ps && docker compose logs --tail=50`

## Command Reference

- `python3 scripts/deploy.py deploy [flags]` – full deployment pipeline.
- `python3 scripts/deploy.py status` – prints cached state + `docker compose ps`.
- `python3 scripts/deploy.py rollback` – tears down containers, repos, and artifacts.
- `docker compose logs -f oss-fuzz-runner` – follow orchestrator logs live.
- `docker compose exec oss-fuzz-runner bash` – open an interactive shell for quick experiments.
- `docker compose build --pull` – rebuild images with the latest upstream layers.

## Possible Issues & Fixes

- **Docker daemon unavailable** – Ensure the service is running (`sudo systemctl start docker`) or add your user to the `docker` group, then re-run deploy.
- **`config/fuzz_targets.yaml` missing or malformed** – Copy the sample file or validate YAML syntax (`python3 -c "import yaml,sys; yaml.safe_load(open('config/fuzz_targets.yaml'))"`).
- **Long image build times** – Use `python3 scripts/deploy.py deploy --skip-build` when only configuration changed, or enable Docker BuildKit caching (`DOCKER_BUILDKIT=1`).
- **Helper script crashes on build** – Inspect `artifacts/<target>/build.log` for compiler errors; often missing dependencies inside `oss-fuzz` project files.
- **Runner exits immediately** – Verify at least one target has `enabled: true`; otherwise the orchestrator raises `No enabled targets found`.
- **Port exhaustion / resource pressure** – Lower `--max-parallel`, cap CPU usage via Compose (`cpus: 4`), or run on a larger host.
- **Permission denied on repo cloning** – Check workspace ownership; the deploy script expects write access to `/workspace`.
- **Network flakes while cloning** – Re-run with `--force-reclone` to delete partial checkouts.
- **Docker Compose command not found** – Install the plugin via `sudo apt-get install docker-compose-plugin` or use a recent Docker release that bundles it.
- **Crash artifacts piling up** – Periodically `rm -rf artifacts/*` or leverage `python3 scripts/deploy.py rollback` to start fresh.