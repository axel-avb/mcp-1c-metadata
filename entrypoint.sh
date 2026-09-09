#!/bin/sh
# Container entrypoint: optionally build the index on start, then run the server.
#
#   docker compose up -d                 # index (if not built) + serve
#   docker compose run --rm mcp index    # full rebuild only
#   docker compose run --rm --no-deps mcp python -m src.indexer --full
#
# Set AUTO_INDEX=0 to skip the on-start indexing.

set -e

cd /app

MODE="${1:-serve}"

run_index() {
  # Skip if the graph already has nodes and AUTO_INDEX is not forced full.
  if [ "${AUTO_INDEX:-1}" = "0" ]; then
    echo "entrypoint: AUTO_INDEX=0, skipping index"
    return 0
  fi
  if [ -f "${ONEC_GRAPH_DB_PATH:-/data/graph.sqlite3}" ] \
     && [ -s "${ONEC_GRAPH_DB_PATH:-/data/graph.sqlite3}" ]; then
    echo "entrypoint: graph db exists, running incremental index"
    python -m src.indexer
  else
    echo "entrypoint: no graph db, running full index"
    python -m src.indexer --full
  fi
}

case "$MODE" in
  index)
    run_index
    ;;
  serve)
    run_index
    exec python -m src.server
    ;;
  *)
    # passthrough: `docker compose run mcp <cmd...>`
    exec "$@"
    ;;
esac
