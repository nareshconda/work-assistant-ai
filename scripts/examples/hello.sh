#!/usr/bin/env bash
# @name: hello
# @description: A smoke test that proves script running works end to end. Prints a greeting and the current time.
# @arg name: Who to greet. Defaults to "world".
echo "Hello, ${1:-world}!"
echo "The time here is $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "Running as $(whoami) in $(pwd)"
