#!/usr/bin/env bash
# entrypoint.sh — start Xvfb on :99 (so the "headed" Chromium spawned by
# browser-use has a virtual X display to attach to) then exec the actual
# command. Headless is still the default DISPLAY-less path; this just
# means we *can* go headed later by flipping a flag.
set -e

if [ -z "${DISPLAY:-}" ]; then
    Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp &
    export DISPLAY=:99
    # Tiny settle so Chromium doesn't race the X server.
    sleep 0.3
fi

exec "$@"
