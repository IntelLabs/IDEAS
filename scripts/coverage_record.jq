. as $report
| ($report.cargo_llvm_cov.manifest_path | split("/")) as $path
| ($root | split("/") | length) as $root_depth
| {
    project: ($path[$root_depth:-3] | join("/")),
    crate: $path[-2],
    metrics: $report.data[0].totals
  }
