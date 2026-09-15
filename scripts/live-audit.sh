#!/usr/bin/env bash
# Drive every command the way a person does, and check the exit code.
#
# The unit tests prove the pieces; this proves the thing you actually type.
# Run it before the demo - it takes a few seconds and catches the class of
# break that only shows up through the real argument parser.
#
# Exit codes are the contract:
#   0  PASS      1  setup error      2  FAIL or INDETERMINATE      3  audit
#
# A non-compliant kit exiting 2 rather than 0 is the point: a script that
# treated "the latch stayed shut" as success would be the fail-open mistake
# this whole project argues against.

cd "$(dirname "$0")/.." || exit 1
R=.venv/bin/readykit
M=manifests/trauma-kit-a.json
pass=0; fail=0
run() {
  desc="$1"; expect="$2"; shift 2
  out=$("$@" 2>&1); code=$?
  if [ "$code" = "$expect" ]; then
    printf "  ok   %-52s exit %s\n" "$desc" "$code"; pass=$((pass+1))
  else
    printf "  FAIL %-52s exit %s (wanted %s)\n" "$desc" "$code" "$expect"
    printf "       %s\n" "$(echo "$out" | tail -2 | head -1)"; fail=$((fail+1))
  fi
}
echo "--- happy paths ---"
run "scenes"                       0 $R scenes
run "doctor"                       1 $R doctor
run "inspect complete"             0 $R inspect --manifest $M --scene complete --no-log
run "inspect missing (FAIL)"       2 $R inspect --manifest $M --scene missing-shears --no-log
run "inspect occluded (INDET)"     2 $R inspect --manifest $M --scene occluded --no-log
run "inspect expired"              2 $R inspect --manifest $M --scene expired --no-log
run "inspect short (quantity)"     2 $R inspect --manifest $M --scene short --no-log
run "inspect garbled"              2 $R inspect --manifest $M --scene garbled --no-log
run "inspect engine-fault"         2 $R inspect --manifest $M --scene engine-fault --no-log
run "inspect other manifest"       0 $R inspect --manifest manifests/electrical-toolbox.json --scene complete --no-log
run "watch --limit 3"              0 $R watch --manifest $M --scene complete --limit 3 --no-log
run "sentinel --limit 4"           0 $R sentinel --manifest $M --scene complete --limit 4
run "compare"                      0 $R compare --manifest $M
run "bench --runs 5"               0 $R bench --manifest $M --runs 5
run "demo --dwell 0"               0 $R demo --manifest $M --dwell 0
run "records"                      0 $R records
echo "--- error paths (should fail cleanly, never traceback) ---"
run "missing manifest file"        1 $R inspect --manifest nope.json --scene complete --no-log
run "unknown scene"                2 $R inspect --manifest $M --scene not-a-scene --no-log
run "geniex without SDK"           1 $R inspect --manifest $M --ffmpeg-camera 0 --engine geniex --no-log
run "ollama with scene name"       1 $R inspect --manifest $M --engine ollama --scene complete --no-log
run "console on routable host"     2 $R console --manifest $M --host 0.0.0.0
run "image that does not exist"    2 $R inspect --manifest $M --image /tmp/nope.jpg --engine simulated --no-log
run "serial to a dead port"        1 $R inspect --manifest $M --scene complete --link serial --port /dev/nope --no-log
run "audit on absent log"          3 $R audit --log /tmp/definitely-absent.jsonl
echo
echo "  $pass passed, $fail failed"
