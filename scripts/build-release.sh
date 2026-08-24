#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-$ROOT/release}"
SHORT_HEAD=$(git -C "$ROOT" rev-parse --short=12 HEAD)
VERSION="${SPONSORFLOW_RELEASE_VERSION:-$(date -u +%Y%m%d)-$SHORT_HEAD}"
ARCHIVE="$OUTPUT_DIR/sponsorflow-$VERSION.tar.gz"

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR=$(readlink -f "$OUTPUT_DIR")
case "$OUTPUT_DIR" in "$ROOT"|"$ROOT"/*) ;; *) ;; esac

stage=$(mktemp -d)
list=$(mktemp)
trap 'rm -rf "$stage" "$list"' EXIT
mkdir -p "$stage/sponsorflow"

cd "$ROOT"
raw_list=$(mktemp)
trap 'rm -rf "$stage" "$list" "$raw_list"' EXIT
git ls-files -z --cached --others --exclude-standard > "$raw_list"
while IFS= read -r -d '' path; do
  if [ -f "$path" ] || [ -L "$path" ]; then
    printf '%s\0' "$path" >> "$list"
  fi
done < "$raw_list"
if [ ! -s "$list" ]; then
  echo "release file list is empty" >&2
  exit 1
fi

tar --null --files-from="$list" -cf - | tar -xf - -C "$stage/sponsorflow"
cat > "$stage/sponsorflow/RELEASE-MANIFEST.txt" <<EOF
SponsorFlow source release
version=$VERSION
git_commit=$(git rev-parse HEAD)
git_branch=$(git branch --show-current)
contains_git_history=false
contains_runtime_credentials=false
production_provider_mode=live
selected_llm_provider=nvidia_nim
selected_llm_model=deepseek-ai/deepseek-v4-flash-0731
EOF

python3 "$ROOT/scripts/check-release.py" "$stage/sponsorflow"
(
  cd "$stage/sponsorflow"
  find . -type f ! -name FILE-MANIFEST.sha256 -print0 \
    | sort -z \
    | xargs -0 sha256sum > FILE-MANIFEST.sha256
)

mkdir -p "$OUTPUT_DIR"
rm -f "$ARCHIVE" "$ARCHIVE.sha256"
SOURCE_DATE_EPOCH=$(git show -s --format=%ct HEAD)
tar --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner \
  -C "$stage" -cf - sponsorflow | gzip -n > "$ARCHIVE"
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"
chmod 600 "$ARCHIVE" "$ARCHIVE.sha256"
printf 'release_archive=%s\nrelease_checksum=%s\n' "$ARCHIVE" "$ARCHIVE.sha256"
