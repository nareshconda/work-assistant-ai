#!/usr/bin/env bash
# @name: cleanup_demo
# @description: EXAMPLE of a destructive script. Does nothing real - it only prints what it would delete - but shows how @confirm makes the assistant ask you first. Copy this pattern for anything that actually removes data.
# @arg path: Folder it would clean. Defaults to /tmp/example.
# @confirm: true
# @timeout: 30
TARGET="${1:-/tmp/example}"
echo "Would remove temporary files under: $TARGET"
echo "(this example deletes nothing)"
