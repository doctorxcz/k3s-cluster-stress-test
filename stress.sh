#!/usr/bin/env bash
# ./stress.sh [options]  ->  python3 -m stress_test [options]
proj="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

if [ ! -d "$proj/stress_test" ]; then
  echo "stress.sh: package stress_test not found in $proj" >&2
  exit 1
fi
PYTHONPATH="$proj${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m stress_test "$@"
