#!/bin/sh
# PID 1 for the quip-syslog collector: runs syslog-ng and rotates the merged
# log at QUIP_LOG_MAX_BYTES.
#
# Why a supervisor rather than cron: syslog-ng OSE has no size-based rotation,
# this image ships no logrotate, and setting a custom entrypoint bypasses the
# image's s6 supervisor -- so crond never starts. This loop replaces it.
set -eu

LOG=/logs/quip-node.log
MAX_BYTES="${QUIP_LOG_MAX_BYTES:-10485760}"   # 10 MB, matching v0.1 node_log
KEEP="${QUIP_LOG_KEEP:-5}"
INTERVAL="${QUIP_LOG_CHECK_INTERVAL:-30}"

syslog-ng -F -f /config/syslog-ng.conf &
SNG=$!

RUNNING=1
SLP=""
# The trap MUST kill the backgrounded sleep as well. POSIX sh does not run
# traps while a foreground sleep blocks, so a plain `sleep $INTERVAL` here
# makes the container ignore SIGTERM until docker SIGKILLs it 10s later,
# discarding whatever syslog-ng still holds in its output buffer.
stop() {
    RUNNING=0
    kill -TERM "$SNG" 2>/dev/null || true
    [ -n "$SLP" ] && kill "$SLP" 2>/dev/null || true
}
trap stop TERM INT

# Rename-then-SIGHUP, not copytruncate: syslog-ng holds an open descriptor at
# a byte offset, so truncating in place leaves a sparse hole the size of the
# old log. SIGHUP makes syslog-ng reopen the path and create a fresh file.
rotate() {
    rm -f "$LOG.$KEEP"
    i=$((KEEP - 1))
    while [ "$i" -ge 1 ]; do
        [ -f "$LOG.$i" ] && mv "$LOG.$i" "$LOG.$((i + 1))"
        i=$((i - 1))
    done
    mv "$LOG" "$LOG.1"
    kill -HUP "$SNG" || true
}

# Set once the file has been observed to exist, so the very first iteration
# (before syslog-ng has received a line and created the file) does not read
# as a deletion and trigger a needless respawn.
SEEN=0

while [ "$RUNNING" -eq 1 ] && kill -0 "$SNG" 2>/dev/null; do
    if [ -e "$LOG" ]; then
        SEEN=1
        if [ "$(stat -c %s "$LOG")" -ge "$MAX_BYTES" ]; then
            rotate
        fi
    elif [ "$SEEN" -eq 1 ]; then
        # The path was unlinked (e.g. `rm data/logs/quip-node.log`) while
        # syslog-ng still holds the old inode open, so it keeps writing to
        # nothing the filesystem shows and rotation can never fire again.
        # SIGHUP does not recover this -- only reopening the process does.
        kill -TERM "$SNG" 2>/dev/null || true
        wait "$SNG" 2>/dev/null || true
        syslog-ng -F -f /config/syslog-ng.conf &
        SNG=$!
    fi
    # Backgrounded sleep plus wait: `wait` is interruptible by signals, so
    # SIGTERM reaches the trap at once instead of after INTERVAL seconds.
    sleep "$INTERVAL" & SLP=$!
    wait "$SLP" 2>/dev/null || true
done

wait "$SNG" 2>/dev/null || true
