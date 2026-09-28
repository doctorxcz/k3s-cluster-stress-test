#!/usr/bin/env bash
# ./stress.sh [options]  ->  python3 -m stress_test [options]
proj="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# if this is a plain copy placed in ~/cluster-testing, take the project from there
[ -d "$proj/stress_test" ] || proj="$HOME/cluster-testing/python-stress-test-en"
if [ ! -d "$proj/stress_test" ]; then
  echo "stress.sh: package stress_test not found in $proj" >&2
  exit 1
fi
PYTHONPATH="$proj${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m stress_test "$@"
