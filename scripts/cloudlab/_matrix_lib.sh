# scripts/cloudlab/_matrix_lib.sh — sourceable helpers for matrix launchers.
#
# Cell-level skip support: if (transport, loss, seed) already has a Hydra
# run dir with a fully-completed loss_per_step.csv (header + STEPS rows),
# the launcher should skip that cell on restart.  Lets matrices survive
# matrix-process death / node reboot losing at most one in-flight cell.
#
# Caveat: the Hydra run dir name encodes only (transport, loss, seed), not
# (ratio, timeout, chunk_bytes, steps).  Two runs of the same triple with
# different transport_cfg overrides will collide at the cell-skip layer.
# Mitigation: per-launcher constants for ratio/timeout/chunk are documented
# in the launcher header; if you change them, move/clear the prior result
# tree before rerunning, or pass a different RESULT_ROOT_FILTER.
#
# Usage (in launcher script):
#   source "$(dirname "$0")/_matrix_lib.sh"
#   if cell_already_done "$transport" "$loss" "$seed" "$STEPS"; then
#       echo "  SKIP cell #$cell_idx ($transport loss=$loss seed=$seed): already complete"
#       cell_idx=$((cell_idx + 1))
#       continue
#   fi

# Default location of the Hydra results tree.  Override via env if needed.
: "${RESULT_ROOT:=$HOME/SemiRDMA/experiments/results/stage_b}"

# cell_already_done <transport> <loss> <seed> <expected_steps>
# Returns 0 (true) iff there is at least one run dir matching the triple
# whose loss_per_step.csv has (expected_steps + 1) lines.  Prints the
# matched dir to stderr for transparency.
cell_already_done() {
    local transport="$1"
    local loss="$2"
    local seed="$3"
    local steps="$4"
    local want_lines=$((steps + 1))   # +1 for CSV header

    # Run dir convention (set by Hydra in stage_b_cloudlab.yaml):
    #   <date>/<HH-MM-SS>_<transport>_loss<L>_seed<S>/
    # Glob across all date dirs; tolerate concurrent partial cells with
    # short CSVs by requiring the exact line count.
    local match
    match=$(find "$RESULT_ROOT" -mindepth 2 -maxdepth 2 -type d \
        -name "*_${transport}_loss${loss}_seed${seed}" 2>/dev/null \
        | while IFS= read -r d; do
            local n
            n=$(wc -l < "$d/loss_per_step.csv" 2>/dev/null || echo 0)
            if [ "$n" -eq "$want_lines" ]; then
                printf '%s\n' "$d"
            fi
          done | head -1)
    if [ -n "$match" ]; then
        printf '    found prior complete result: %s\n' "$match" >&2
        return 0
    fi
    return 1
}
