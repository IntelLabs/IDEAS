select(.type == "test" and (.event == "ok" or .event == "failed"))
| if .event == "ok" then "ok"
  elif .reason == "time limit exceeded" then "TIMEOUT"
  else "FAILED"
  end
