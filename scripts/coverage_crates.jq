def metric_names: ["branches", "functions", "instantiations", "lines", "mcdc", "regions"];
def coverage($metric):
  if $metric.count > 0 then (100 * $metric.covered / $metric.count) else null end;
def format_percent:
  if . == null then "N/A" else "\((. * 100 | round) / 100)%" end;
sort_by([.project, .crate])[]
| . as $crate
| "\($crate.project)/\($crate.crate) "
  + ([metric_names[] as $name
      | ($crate.metrics[$name] // {count: 0, covered: 0}) as $metric
      | "\($name)=\($metric.covered)/\($metric.count)(\(coverage($metric) | format_percent))"] | join(" "))
