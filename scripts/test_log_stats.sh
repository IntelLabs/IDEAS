#!/bin/sh

# Try one jq for the whole batch and fallback to per-record if any log is malformed
FILTER='select(.type=="suite" and .event!="started") | "\(input_filename) \(.passed // 0) \(.failed // 0)"'
STATS=$(jq -r "$FILTER" "$@" 2>/dev/null)
if [ $? -ne 0 ]; then
  STATS=$(for LOG in "$@"; do jq -r "$FILTER" "$LOG" 2>/dev/null; done)
fi

# Print "STATUS path" per log, listing the logs first so ones jq skipped still get reported
{ printf '%s\n' "$@"; printf '%s\n' "$STATS"; } | awk '
  # Single-field lines are the log paths, kept in argument order
  NF==1 { logs[++n]=$0; next }

  # Sum every suite in a log, since one log can hold several test binaries
  { pass[$1]+=$2; fail[$1]+=$3 }

  END {
    for (i = 1; i <= n; i++) {
      f = logs[i]; p = pass[f]+0; q = fail[f]+0

      # No tests at all is MISSING, all passing is complete, a mix is PARTIAL, none passing is FAILED
      print (p==0 && q==0 ? "MISSING" : q==0 ? "complete" : p>0 ? "PARTIAL" : "FAILED"), f
    }
  }'
