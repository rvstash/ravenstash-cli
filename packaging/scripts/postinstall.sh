#!/bin/sh
set -eu

rocm_root="${RAVENSTASH_ROCM_ROOT:-/opt}"
rocm_rvs=""

for candidate in \
  "$rocm_root"/rocm*/bin/rvs \
  "$rocm_root"/rocm*/extras-*/bin/rvs
do
  if [ -x "$candidate" ]; then
    rocm_rvs="$candidate"
    break
  fi
done

if [ -n "$rocm_rvs" ]; then
  printf '%s\n' \
    "Ravenstash CLI notice:" \
    "  AMD ROCm Validation Suite was detected at $rocm_rvs." \
    "  Both tools provide the 'rvs' shortcut; PATH order selects which one runs." \
    "  Ravenstash CLI is also available through the 'ravenstash' command."
fi
