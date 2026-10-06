# Contributing

## Workflow

- `main` is always working. Nothing is committed directly to `main`; every
  change goes through a pull request.
- Branch from an up-to-date `main`:

  ```bash
  git switch main
  git pull
  git switch -c feature/short-description
  ```

  Branch prefixes: `feature/`, `bugfix/`, `docs/`, `test/`, `chore/`,
  `refactor/`, `spike/` (throw-away experiments).
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):

  ```text
  feat(car-node): add per-channel rate scheduler
  fix(protocol): reject packets with truncated payload
  docs(bandwidth): add LoRa SF7 airtime estimate
  test(pit): cover sequence-number wraparound
  ```

  Useful scopes: `sim`, `dbc`, `car-node`, `protocol`, `transport`,
  `link-sim`, `pit`, `alerts`, `infra`, `grafana`, `replay`, `bandwidth`,
  `docs`.
- Keep pull requests small enough to review properly (roughly under 400
  changed lines where possible). Squash-merge feature branches.
- Never force-push `main` or a branch someone else is working on. On your own
  branch, use `git push --force-with-lease`, never `--force`.

## Rules specific to this project

1. **The car node never transmits on CAN.** It is a passive listener. Do not
   add `send()` calls on the vehicle bus. The only components allowed to write
   CAN frames are the fake ECU and the replay tool, and only onto a virtual bus.
2. **Simulated CAN IDs must be labelled SIMULATED** in the DBC and docs. Never
   present them as real UVFR IDs.
3. **No secrets in git.** Credentials live in `.env` (git-ignored); document new
   variables in `.env.example`.
4. **Real test-day recordings stay out of git** unless the team has agreed a
   specific, small sample can be committed under `tests/fixtures/`.
5. **Thresholds live in configuration** (`config/alerts.yaml`), not in code or
   hand-edited dashboard JSON.
6. **Line endings are LF** everywhere (enforced by `.gitattributes`). The code
   runs on Linux; CRLF breaks shell scripts.

## Development environments

You do not need access to the homelab to work on this project.

| Environment | What works |
|---|---|
| Homelab telemetry VM (Debian 13) | Everything, including vcan0 and the full demo |
| Any Linux machine with the `vcan` kernel module + Docker | Everything (`./scripts/setup-vcan.sh`, then `make demo`) |
| WSL2 / macOS / Windows without SocketCAN | `make test`, and the pipeline over python-can's UDP-multicast virtual bus instead of vcan: `pip install msgpack`, then `python -m simulator --interface udp_multicast` and `python -m car_node --interface udp_multicast` (verified on Linux; expected but not yet verified on macOS and Windows) |

Working from Windows against the lab VM: edit locally, then
`scripts/deploy.sh uvfr-lab` (Git Bash) copies your working tree to the VM,
leaving its `.env`, venv and recordings alone. Run things there over SSH.

## Before opening a pull request

- `make test` passes (CI runs it on Python 3.11 and 3.13 for every pull request).
- If you touched CAN, protocol, transport, the pit receiver, alarms or
  InfluxDB writing: `make test-all` on a vcan-capable host with the backend
  running (the lab VM).
- If you changed `config/dashboard.yaml`, `config/alerts.yaml` or channel
  names: `make dashboard` and commit the regenerated JSON.
- `git diff --staged` reviewed: no debug output, secrets or recordings.
