BEGIN { OFS = " " }
{
  key = $1 OFS $2
  if (key != previous && previous != "") print output
  if (key != previous) { output = "EXHAUSTED " $1 " " $2; previous = key }
  if (!seen[key SUBSEP $3]++) output = output " `" $3 "`"
}
END { if (previous != "") print output }
