#!/usr/bin/env bash
# Clone the pinned Apache Fineract tag into fineract-customization/fineract and
# apply our patch on a working branch. Set FINERACT_MIRROR to clone from a local copy.
set -euo pipefail
TAG="${FINERACT_TAG:-1.15.0}"
SRC="${FINERACT_MIRROR:-https://github.com/apache/fineract.git}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/fineract-customization/fineract"
PATCH="$(dirname "$DEST")/patches/erp-gl-sync.patch"

if [ ! -d "$DEST/.git" ]; then
  git clone --branch "$TAG" --depth 1 "$SRC" "$DEST"
fi
cd "$DEST"
git checkout -q -B erp-gl-sync "$TAG"
if [ -s "$PATCH" ]; then
  git apply --index "$PATCH"
  echo "Applied $(basename "$PATCH") on $TAG"
else
  echo "No patch yet; working tree is clean $TAG"
fi
