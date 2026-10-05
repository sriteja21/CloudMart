#!/usr/bin/env bash
# Usage: stack-params.sh <template.yml> <parameters.json> [Key=Value ...]
# Prints one Key=Value line for every parameter DECLARED by the template,
# taking values from the JSON file, then from the Key=Value overrides
# (secrets / computed values). Parameters the template does not declare are
# skipped, so one shared parameters.json can feed every stack.
set -euo pipefail
tpl=$1; json=$2; shift 2

declared=$(awk '/^Parameters:/{f=1;next} /^[A-Za-z]/{f=0}
  f && /^  [A-Za-z][A-Za-z0-9]*:[[:space:]]*$/{sub(/:.*/,"");gsub(/ /,"");print}' "$tpl")

declare -A val
while IFS=$'\t' read -r k v; do val[$k]=$v; done < <(
  jq -r '.[] | [.ParameterKey, .ParameterValue] | @tsv' "$json")
for kv in "$@"; do val[${kv%%=*}]=${kv#*=}; done

for p in $declared; do
  if [[ -z "${val[$p]+x}" || -z "${val[$p]}" ]]; then
    echo "ERROR: parameter '$p' required by $tpl has no value" >&2; exit 1
  fi
  printf '%s=%s\n' "$p" "${val[$p]}"
done