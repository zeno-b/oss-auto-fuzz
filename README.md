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

## Sample Target Catalog

The repository ships with a handful of pre-wired targets that demonstrate common
bug classes and sanitizer configurations:

- **`libpng_decoder`** – baseline image parser with `address` sanitizer and a
  tuned dictionary for file-format tokens.
- **`libxslt_xpath`** – XML + XPath stress test that benefits from deeper stack
  traces and custom ASAN options.
- **`ffmpeg_mov_demuxer`** – media demuxer hammered under `memory` sanitizer
  with oversized buffers to stress vectorized codecs.
- **`wolfssl_dtls`** – network-style harness that keeps `undefined` sanitizer
  enabled to flush UB in cryptographic state machines.
- **`sqlite_shell`** – disabled by default, but demonstrates how to park targets
  in the config until you are ready to enable them.

Enable or tweak any of these entries, then re-run deploy or call the
orchestrator directly inside the runner container to pick up the changes.

## Example Commands

These snippets cover the most common day-to-day actions:

```bash
# Spin up everything, installing Docker/Git if missing
python3 scripts/deploy.py deploy --auto-install

# Force a clean oss-fuzz checkout and rebuild images from scratch
python3 scripts/deploy.py deploy --force-reclone

# Tail the libpng fuzzer log as it runs
docker compose exec oss-fuzz-runner tail -f /workspace/artifacts/libpng_decoder/run.log

# Run orchestrator manually with debug logs and limited parallelism
docker compose exec oss-fuzz-runner python3 /workspace/scripts/fuzz_orchestrator.py \
  --log-level DEBUG --max-parallel 2

# Reproduce a crash inside the builder container
docker compose run --rm oss-fuzz-builder \
  bash -lc "./infra/helper.py reproduce libpng libpng_read_fuzzer artifacts/libpng_decoder/crashes/id:000000"
```

## Hands-on Tutorial: Hunting an RCE in libpng

The workflow below mirrors how you could discover and triage a critical bug (for
example, an RCE primitive) in a memory-unsafe parser such as `libpng`. Adjust
project names or sanitizers to match your real target.

1. **Deploy the fuzzing stack**
   ```bash
   python3 scripts/deploy.py deploy --auto-install
   ```
   This bootstraps dependencies, clones `oss-fuzz`, builds the containers, and
   launches the builder/runner pair.

2. **Review and customize the target entry**  
   Open `config/fuzz_targets.yaml` and inspect `libpng_decoder`. Tune
   `environment`, `dictionary`, or `max_run_seconds` to guide the search (e.g.,
   set `UBSAN_OPTIONS=print_stacktrace=1:silence_unsigned_overflow=0`).

3. **Kick off a focused fuzzing session**  
   Inside the runner container, call the orchestrator for a single target:
   ```bash
   docker compose exec oss-fuzz-runner python3 /workspace/scripts/fuzz_orchestrator.py \
     --config /workspace/config/fuzz_targets.yaml --max-parallel 1
   ```
   The script builds `libpng`, runs the harness, and streams logs to
   `artifacts/libpng_decoder/run.log`.

4. **Monitor coverage and crashes in real time**
   ```bash
   tail -f artifacts/libpng_decoder/run.log
   ```
   Look for sanitizer violations such as `heap-buffer-overflow` or
   `stack-use-after-return`. Crash artifacts land in
   `artifacts/libpng_decoder/crashes/`.

5. **Reproduce the crash with helper.py**  
   Use the upstream helper to verify determinism and gather stack traces:
   ```bash
   docker compose run --rm oss-fuzz-builder \
     bash -lc "./infra/helper.py reproduce libpng libpng_read_fuzzer \
     artifacts/libpng_decoder/crashes/id:000000"
   ```
   Attach `gdb` or `lldb` inside the container if you need richer debugging.

6. **Minimize the input and isolate the bug class**
   ```bash
   docker compose run --rm oss-fuzz-builder \
     bash -lc "./infra/helper.py minimize_corpus libpng libpng_read_fuzzer \
     --crash artifacts/libpng_decoder/crashes/id:000000 \
     --corpus artifacts/libpng_decoder/corpus"
   ```
   Smaller proofs-of-concept make it easier to reason about parser logic and to
   craft payloads for downstream exploit scenarios.

7. **Evaluate exploitability for RCE**  
   Translate the memory bug into a remote trigger. For an image decoder, that
   often means embedding the crashing chunk into a file type that a client or
   server will automatically parse. Pair the minimized input with a harness that
   mirrors the real product (e.g., a web server thumbnailer) to confirm control
   over instruction pointer or heap metadata.

8. **Patch, harden, and rerun**  
   Apply candidate fixes inside `oss-fuzz/projects/libpng`, rebuild with
   `docker compose run --rm oss-fuzz-builder bash -lc "./infra/helper.py build_fuzzers libpng"`,
   then rerun step 3 to ensure the issue is resolved.

9. **Report responsibly**  
   Follow the target project's disclosure policy, coordinate CVE assignment if
   the bug yields RCE, and contribute the minimized corpus file back to improve
   regression coverage.

The same loop works for the other sample targets (e.g., `wolfssl_dtls` for
network-facing cryptographic code or `ffmpeg_mov_demuxer` for media pipelines);
only the harness names and reproduction arguments change.

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