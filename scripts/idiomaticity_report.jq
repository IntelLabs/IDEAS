if $report == "mean" then
  .dimensions | to_entries[] | "\(.key)\t\(.value.level)"
elif $report == "detail" then
  "\(input_filename) \(.crate) \([.dimensions | keys_unsorted[] as $key | "\($key[0:2] | ascii_upcase)=\(.[$key].level)"] | join(" ")) mean=\(.mean_level * 100 | round / 100)"
elif $report == "gate-status" then
  if ([.gate_findings[].tool] | any(. == "rapx")) then "UB-GATED"
  elif (.gate_findings | length) > 0 then "GATED"
  else "ungated"
  end
elif $report == "gate-findings" then
  select((.gate_findings | length) > 0)
  | "GATED \(input_filename) \(.crate) \(.gate_findings | length) finding(s): \([.gate_findings[].lint] | unique | join(", "))"
elif $report == "ub-status" then
  .ub_scan | if . == "clean" or . == "skipped" then . else ascii_upcase end
elif $report == "ub-findings" then
  select(.ub_scan != "clean" and .ub_scan != "skipped")
  | "\(.ub_scan | ascii_upcase) \(input_filename) \(.crate)"
elif $report == "usage" then
  .usage // {}
  | [.cost // 0, .total_tokens // 0, .prompt_tokens // 0, .completion_tokens // 0, .cost_missing // 0]
  | @tsv
else
  error("unknown idiomaticity report: \($report)")
end
