#!/bin/bash
# something.sh
# This script is piped via stdin to the remote server using:
#   ssh user@server 'bash -s -- "python run.py --dir=/home" "arg1" "arg2"' < something.sh
#
# Inside this script:
#   $1 = full command string  (e.g. "python run.py --dir=/home")
#   $2 = 인자1
#   $3 = 인자2
#   ...

set -euo pipefail

CMD="$1"
shift

echo "[remote] working dir: $(pwd)"
echo "[remote] command: $CMD"
echo "[remote] extra args: $*"

# Execute the command with remaining positional arguments
eval "$CMD" "$@"
