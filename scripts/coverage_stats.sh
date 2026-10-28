#!/bin/sh
#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#
# Pretty-print per-project branch coverage plus the median and the average.
# Takes coverage TSV paths as arguments or, when given none, on stdin.

if [ "$#" -gt 0 ]; then
  FILES=$(printf '%s\n' "$@")
else
  FILES=$(cat)
fi

[ -n "$FILES" ] || { echo "  no coverage data"; exit 0; }

# The bare project line keeps projects with an empty tsv in the table
printf '%s\n' "$FILES" | while IFS= read -r TSV; do
  PROJECT=${TSV%/*}
  PROJECT=${PROJECT%/*}
  printf '%s\t\n' "$PROJECT"
  [ -s "$TSV" ] && sed "s|^|$PROJECT	|" "$TSV"
done | awk -F'\t' '
  {
    project = $1
    if (!(project in seen)) { seen[project] = 1; order[++projects] = project }
    if ($2 == "") next
    coverage = $3; sub(/%$/, "", coverage)
    if (coverage ~ /^[0-9]+(\.[0-9]+)?$/) { sum[project] += coverage; covered[project]++ }
    else                                   { missing[project]++ }
  }

  END {
    for (i = 1; i <= projects; i++) {
      p = order[i]; crates = covered[p] + missing[p]
      if (covered[p] > 0) {
        value = sum[p] / covered[p]; values[++n] = value; total += value
        printf "  %-60s %7.2f%%  (%d/%d crates)\n", p, value, covered[p], crates
      } else {
        printf "  %-60s %8s  (%d/%d crates)\n", p, "N/A", 0, crates
      }
    }

    if (n == 0) { print "  no coverage data"; exit }

    # Insertion sort, small n and portable across awk implementations
    for (i = 2; i <= n; i++) {
      value = values[i]
      for (j = i - 1; j > 0 && values[j] > value; j--) values[j + 1] = values[j]
      values[j + 1] = value
    }
    median = (n % 2) ? values[(n + 1) / 2] : (values[n / 2] + values[n / 2 + 1]) / 2

    printf "\n"
    printf "  %-60s %5d/%d\n", "Projects with branch coverage:", n, projects
    printf "  %-60s %7.2f%%\n", "Average branch coverage:", total / n
    printf "  %-60s %7.2f%%\n", "Median branch coverage:", median
  }'
