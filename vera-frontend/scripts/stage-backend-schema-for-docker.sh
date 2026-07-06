#!/usr/bin/env bash
# src/lib/ibv/*.ts import the backend's schema JSON directly (single source of
# truth, no bundled copy) via a path that reaches outside this directory into
# the sibling vera-backend/. `docker build`'s context is vera-frontend/ only,
# so that import can't resolve inside the container. Run this before `docker
# build` (the frontend Dockerfile expects it) to stage a copy inside the build
# context, at the same relative layout, so it lands at the path those imports
# expect once copied into the image.
set -euo pipefail

FRONTEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEMA_REL_PATH="data/form_schemas/ibv_form_standard_v2.json"
SRC="$FRONTEND_DIR/../vera-backend/$SCHEMA_REL_PATH"
DEST="$FRONTEND_DIR/.docker-backend-data/$SCHEMA_REL_PATH"

mkdir -p "$(dirname "$DEST")"
cp "$SRC" "$DEST"
