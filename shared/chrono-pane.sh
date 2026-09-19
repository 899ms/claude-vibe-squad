#!/usr/bin/env bash
# One home for "is the coordinator actually listening in that tmux pane?"
#
# Why this exists: both notifiers asked the question and both got it wrong, in
# different directions.
#
#   * outbox-watcher.sh compared `#{pane_current_command}` to the literal
#     "claude". The real value is the VERSIONED executable name -- measured
#     2026-08-16 on a live pane: "2.1.233". So the guard skipped even while
#     Chrono was running, every time, printing "no notification lost" as it
#     dropped the nudge. That is why board notifications went unseen for a whole
#     session.
#   * squad-monitor.sh had no check at all and typed alerts into whatever held
#     the pane. With Chrono exited that is a shell, which EXECUTES them:
#     "zsh: command not found: git", "zsh: no matches found: (26418 bytes)".
#
# Fixing one and leaving the other is how they drifted apart in the first place,
# so the rule lives here and both source it.
#
# The check is deliberately not a name match. tmux reports the FOREGROUND
# process name, which changes with every Claude release, so any literal is a
# time bomb. Look at real command lines instead -- version-independent.
#
# It checks TWO positions, because `#{pane_pid}` is not always a shell:
#
#   * a CHILD of the pane pid -- the normal case, where launch-squad.sh starts a
#     shell in the pane and Claude runs under it.
#   * the PANE PID ITSELF -- when the pane was respawned with `exec claude`,
#     Claude REPLACES the shell, so it is the pane process and has no shell
#     parent to be a child of.
#
# Measured 2026-09-17: a hand respawn using `exec $SHELL -lc 'claude'` made the
# child-only check fail while Chrono was running normally. Every board nudge was
# skipped for the rest of the session and nothing surfaced the skip, so lane
# completions landed silently and were only noticed by accident. That is the same
# class of silent-skip failure as the versioned-literal bug above, reintroduced
# by an assumption about process shape rather than about a name.

# chrono_pane_resolve_id <tmux-target>
# Prints the pane id (%N) the target names, or nothing when no pane matches.
#
# Verify the target EXISTS before trusting a pid from it. `display-message`
# silently falls back to the ACTIVE pane for an unresolvable target and exits
# 0, so a typo'd or stale target returns the wrong pane's pid and the caller
# cannot tell. Measured 2026-09-17: `-t squad:99.9` returned the live Chrono
# pane's pid with exit 0. Enumerate real panes and require an exact match.
#
# The match must accept every spelling a real caller uses, not just one:
#   * `%N`                    -- the pane id. Hooks inherit TMUX_PANE=%N, and
#                                chrono-focus-gate.sh passes it straight through.
#   * `session:index.pane`    -- what launch-squad.sh and squad-stop.sh use.
#   * `session:name.pane`     -- outbox-watcher.sh's `squad:chrono.0`.
# Measured 2026-09-19: the 2026-09-17 version matched only the index spelling,
# so the focus gate returned "no coordinator" on every one of 293 operator
# prompts and the watcher dropped every board nudge for two days, both silently.
# Window names may contain spaces, so the fields are tab-separated.
chrono_pane_resolve_id() {
    local target="$1"
    local tmux_bin="${TMUX_BIN:-tmux}"
    [[ -n "$target" ]] || return 1
    "$tmux_bin" list-panes -a \
        -F $'#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{session_name}:#{window_name}.#{pane_index}' 2>/dev/null \
        | awk -F '\t' -v t="$target" '$1 == t || $2 == t || $3 == t { print $1; exit }'
}

# chrono_pane_has_coordinator <tmux-target>
# Returns 0 when the coordinator CLI is the live foreground process there.
chrono_pane_has_coordinator() {
    local target="$1" pane_id="" pane_pid="" parent="" pid="" cmd="" executable=""
    local tmux_bin="${TMUX_BIN:-tmux}"

    pane_id="$(chrono_pane_resolve_id "$target")"
    [[ -n "$pane_id" ]] || return 1

    # Ask by pane id, which tmux cannot mis-resolve to some other pane.
    pane_pid="$("$tmux_bin" display-message -p -t "$pane_id" '#{pane_pid}' 2>/dev/null)" || return 1
    [[ -n "$pane_pid" ]] || return 1

    # Use one ps snapshot for every comparison, so hook activity cannot split
    # those observations across separate lookups.
    while read -r pid parent cmd; do
        # Accept either the pane process itself or one of its direct children.
        [[ "$pid" == "$pane_pid" || "$parent" == "$pane_pid" ]] || continue
        executable="${cmd%%[[:space:]]*}"
        # Match the executable path, not a bare word, so an unrelated process
        # that merely mentions "claude" in an argument cannot pass.
        [[ "$executable" == */claude* ]] && return 0
    done < <(ps -eo pid=,ppid=,command= 2>/dev/null)

    # A pane_current_command literal fallback is deliberately absent: tmux
    # reports a versioned executable name here, so an exact match is dead code.
    return 1
}

# chrono_pane_observed_command <tmux-target>
# What is actually there, for a diagnosable skip message.
chrono_pane_observed_command() {
    local target="$1" pane_id=""
    # Same exact-match resolution as the check itself, so the skip message can
    # never describe the active pane while claiming to describe the target.
    pane_id="$(chrono_pane_resolve_id "$target")"
    if [[ -z "$pane_id" ]]; then
        printf 'unavailable'
        return 0
    fi
    "${TMUX_BIN:-tmux}" display-message -p -t "$pane_id" '#{pane_current_command}' 2>/dev/null \
        || printf 'unavailable'
}
