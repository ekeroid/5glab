#!/bin/bash
SSH="ssh -i ~/.ssh/5G-lab-2026 ubuntu@130.235.32.171"

echo "=== 5G SA Core - UE Status ==="
echo ""

# Get active PDU sessions from SMF
SESSIONS=$($SSH "kubectl logs -n open5gs deploy/open5gs-smf --since=30m 2>/dev/null" | \
  grep 'UE SUPI' | grep -v 'Removed' | \
  sed -n 's/.*SUPI\[\([^]]*\)\].*DNN\[\([^]]*\)\].*IPv4\[\([^]]*\)\].*/\1|\2|\3/p' | \
  sort -t'|' -k1 | awk -F'|' '{ last[$1]=$2"|"$3 } END { for(k in last) print k"|"last[k] }' | sort)

# Get gNB-UE count
GNB_UES=$($SSH "kubectl logs -n open5gs deploy/open5gs-amf --tail=100 2>/dev/null" | \
  grep 'Number of gNB-UEs' | tail -1 | grep -oE 'now [0-9]+' | awk '{print $2}')

# Get recent registration failures
FAILURES=$($SSH "kubectl logs -n open5gs deploy/open5gs-amf --since=10m 2>/dev/null" | \
  grep -E 'Registration reject|Cannot find SUCI|AuthenticationFailure' | \
  sed -n 's/.*\[\(suci-[^]]*\)\].*/\1/p; s/.*\[\(imsi-[^]]*\)\].*/\1/p' | \
  sort -u)

echo "Connected UEs (active PDU sessions):"
echo "┌──────────────────────────┬──────────┬─────────────┬──────────┐"
echo "│ IMSI                     │ DNN      │ IP Address  │ Status   │"
echo "├──────────────────────────┼──────────┼─────────────┼──────────┤"

if [ -z "$SESSIONS" ]; then
  echo "│ (none)                   │          │             │          │"
else
  echo "$SESSIONS" | while IFS='|' read -r imsi dnn ip; do
    imsi_clean=$(echo "$imsi" | sed 's/imsi-//')
    printf "│ %-24s │ %-8s │ %-11s │ %-8s │\n" "$imsi_clean" "$dnn" "$ip" "Active"
  done
fi

echo "└──────────────────────────┴──────────┴─────────────┴──────────┘"
echo ""
echo "RAN connections (gNB-UEs): ${GNB_UES:-0}"
echo ""

if [ -n "$FAILURES" ]; then
  echo "Recent failures (last 10 min):"
  echo "$FAILURES" | while read -r line; do
    echo "  - $line"
  done
  echo ""
fi
