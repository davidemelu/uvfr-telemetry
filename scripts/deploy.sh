#!/usr/bin/env bash
# Copy this working tree to a lab host over SSH so it can run there.
#
#   scripts/deploy.sh [ssh-target] [remote-dir]
#
#   ssh-target  default: $UVFR_LAB_HOST, else "uvfr-lab" (an ~/.ssh/config alias)
#   remote-dir  default: $UVFR_LAB_DIR,  else /opt/uvfr-telemetry
#
# Sends tracked files plus new files that are not git-ignored, so .env, .venv,
# recordings and runtime state never leave your machine. Files deleted locally
# are deleted on the remote too; the remote's own .env, .venv, run/, logs/ and
# recordings/ are preserved.
#
# Works from Git Bash on Windows, macOS and Linux. Uncommitted work is included,
# which is the point: test on the VM before you commit.
set -euo pipefail

TARGET="${1:-${UVFR_LAB_HOST:-uvfr-lab}}"
DEST="${2:-${UVFR_LAB_DIR:-/opt/uvfr-telemetry}}"

cd "$(git rev-parse --show-toplevel)"

echo "Deploying $(git rev-parse --abbrev-ref HEAD)@$(git rev-parse --short HEAD) (+ working tree) to $TARGET:$DEST"

git ls-files -z --cached --others --exclude-standard \
    | while IFS= read -r -d '' f; do [[ -e "$f" ]] && printf '%s\0' "$f"; done \
    | tar --null -czf - -T - \
    | ssh "$TARGET" "set -e
        mkdir -p '$DEST'
        stage=\$(mktemp -d)
        trap 'rm -rf \"\$stage\"' EXIT
        chmod 0755 \"\$stage\"   # mktemp's 0700 would otherwise be copied onto the repo dir
        tar -xzf - -C \"\$stage\"
        if command -v rsync >/dev/null; then
            rsync -a --delete \
                --exclude=/.venv/ --exclude=/.env --exclude=/run/ --exclude=/logs/ \
                --exclude=/recordings/ --exclude=__pycache__/ --exclude=/.pytest_cache/ \
                \"\$stage\"/ '$DEST'/
        else
            # Fresh host before provision-vm.sh has installed rsync: copy only.
            cp -a \"\$stage\"/. '$DEST'/
        fi
        find '$DEST/scripts' -name '*.sh' -exec chmod +x {} +
        echo \"deployed \$(find '$DEST' -path '$DEST/.venv' -prune -o -type f -print | wc -l) files\""
