#!/usr/bin/env bash
# @name: slow_demo
# @description: EXAMPLE of a long job. Counts slowly so you can watch output stream in and try the Stop button. Does nothing else.
# @arg seconds: How long to run. Defaults to 60.
# @timeout: 300
N="${1:-60}"
for i in $(seq 1 "$N"); do
  echo "working... step $i of $N"
  sleep 1
done
echo "finished all $N steps"
