def metric_names: ["branches", "functions", "instantiations", "lines", "mcdc", "regions"];
def round2: ((. * 100 | round) / 100);
def coverage($metric):
  if $metric.count > 0 then (100 * $metric.covered / $metric.count) else null end;
def format_percent:
  if . == null then "N/A" else "\(round2)%" end;
# Quartiles use linear interpolation between adjacent sorted values.
def quantile($p):
  if length == 0 then null
  else . as $values
    | ($p * (($values | length) - 1)) as $index
    | ($index | floor) as $low
    | ($index | ceil) as $high
    | ($values[$low] + (($values[$high] - $values[$low]) * ($index - $low)))
  end;
if length == 0 then "No coverage reports found."
else . as $crates
  | (metric_names | map(
      . as $name
      | ([$crates[] | (.metrics[$name] // {count: 0, covered: 0})]) as $totals
      | ([$totals[] | select(.count > 0) | coverage(.)] | sort) as $values
      | ([$totals[].count] | add // 0) as $total
      | ([$totals[].covered] | add // 0) as $covered
      | {
          name: $name,
          available_crates: ($values | length),
          total: $total,
          covered: $covered,
          pooled: (if $total > 0 then (100 * $covered / $total) else null end),
          min: (if ($values | length) > 0 then $values[0] else null end),
          q1: ($values | quantile(0.25)),
          mean: (if ($values | length) > 0 then (($values | add) / ($values | length)) else null end),
          median: ($values | quantile(0.5)),
          q3: ($values | quantile(0.75)),
          max: (if ($values | length) > 0 then $values[-1] else null end)
        }
    )) as $stats
  | ([ (["Metric", "Covered/total", "Pooled", "Min", "Q1", "Mean", "Median", "Q3", "Max", "Crates with data"] | @tsv) ]
    + ($stats | map([
        .name,
        "\(.covered)/\(.total)",
        (.pooled | format_percent),
        (.min | format_percent),
        (.q1 | format_percent),
        (.mean | format_percent),
        (.median | format_percent),
        (.q3 | format_percent),
        (.max | format_percent),
        "\(.available_crates)/\($crates | length)"
      ] | @tsv))
    )
  | join("\n")
end
