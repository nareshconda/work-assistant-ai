#!/usr/bin/env bash
# @name: disk_usage
# @description: Show the largest directories under a path, plus overall free space. Use for "what is filling up the disk" or "how much space is left".
# @arg path: Directory to measure. Defaults to the home directory.
# @timeout: 120
set -uo pipefail

TARGET="${1:-$HOME}"

if [ ! -d "$TARGET" ]; then
  echo "Not a directory: $TARGET"
  exit 1
fi

echo "Largest directories under $TARGET"
echo
du -h -d 1 "$TARGET" 2>/dev/null | sort -hr | head -15

echo
echo "Filesystem usage:"
df -h "$TARGET" 2>/dev/null
