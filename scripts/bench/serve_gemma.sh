#!/usr/bin/env bash
# Start llama-server with the pinned benchmark flags (scripts/bench/README.md, "Reproducing").
#
#   LLAMA_BIN=/path/to/llama-server MODEL=/path/to/model.gguf scripts/bench/serve_gemma.sh [extra flags...]
#
# Extra flags are appended, so a sensitivity run can add --swa-full without editing this file. Every LLAMA_ARG_* variable
# is cleared first: llama-server reads them as flags, and one left in the shell would change the run silently.
set -euo pipefail

: "${LLAMA_BIN:?set LLAMA_BIN to the llama-server binary}"
: "${MODEL:?set MODEL to the gguf file}"

for var in $(env | grep -o '^LLAMA_ARG_[A-Z_]*' || true); do
    unset "$var"
done

# -fit off: fail on a model or context that does not fit, rather than shrinking -c or moving layers to the CPU.
# -np 1: one slot, so every pass of a turn lands on the slot that holds the turn's prefix.
# -cram 4096: host-RAM prompt cache; the 8192 default can run a 32 GB host out of memory with Gemma.
# No --swa-full and the default --ctx-checkpoints: Orb's reuse on sliding-window Gemma is built around checkpoints.
exec "$LLAMA_BIN" \
    -m "$MODEL" \
    --host 127.0.0.1 --port "${PORT:-5000}" \
    -fit off \
    -ngl 99 \
    -c "${CTX:-49152}" \
    -np 1 \
    -b 2048 -ub 512 \
    -fa on \
    -ctk f16 -ctv f16 \
    -cram 4096 \
    --jinja \
    --metrics \
    "$@"
