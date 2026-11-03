{
  colon = index($0, ":")
  path = substr($0, 1, colon - 1)
  message = substr($0, colon + 1)
  needle = "/" dir "/"
  position = index(path, needle)
  if (!position) next
  path = substr(path, 1, position - 1) "/" substr(path, position + length(needle))
  count = split(path, parts, "/")
  project = parts[1]
  for (i = 2; i <= count - 2; i++) project = project "/" parts[i]
  target = parts[count - 1]
  while (match(message, /`[^`]+`/)) {
    symbol = substr(message, RSTART + 1, RLENGTH - 2)
    message = substr(message, RSTART + RLENGTH)
    if (symbol ~ /^c:/) print project "\t" target "\t" symbol
  }
}
