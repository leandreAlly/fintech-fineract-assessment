#!/usr/bin/env bash
# Write the diff between the pinned tag and the working branch (incl. uncommitted changes)
# to fineract-customization/patches/erp-gl-sync.patch.
set -euo pipefail
TAG="${FINERACT_TAG:-1.15.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/fineract-customization/fineract"
git add -A
git diff --cached --binary "$TAG" > "$ROOT/fineract-customization/patches/erp-gl-sync.patch"
echo "Patch: $(wc -l < "$ROOT/fineract-customization/patches/erp-gl-sync.patch") lines"
