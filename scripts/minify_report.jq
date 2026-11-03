if $report == "lines" then
  [.lines_deleted, .lines_before] | @tsv
elif $report == "detail" then
  "\(input_filename) deleted \(.lines_deleted)/\(.lines_before) line(s)"
else
  error("unknown minify report: \($report)")
end
