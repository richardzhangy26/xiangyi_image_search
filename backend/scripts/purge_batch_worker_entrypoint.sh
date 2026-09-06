#!/bin/sh
set -eu
# Root-only volume ownership for capability evidence and worker state.
# Must not load ops env files, write capability payloads, or perform backup work.
chown -R 1000:1000 /app/purge-evidence /var/lib/purge-batch-worker
exec setpriv --reuid=1000 --regid=1000 --init-groups -- "$@"
