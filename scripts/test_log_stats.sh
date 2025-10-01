#!/bin/sh

# Count number of ok/FAILED inside specified file
PASS=`grep -aE "^test \S+ ... ok" $1 | wc -l`
FAIL=`grep -aE "^test \S+ ... FAILED" $1 | wc -l`

# If no PASS nor FAIL, then tests are missing
if [ $PASS -eq 0 ] && [ $FAIL -eq 0 ]; then
  echo MISSING $1

# If some PASS and no FAILs, then consider translation complete
elif [ $PASS -gt 0 ] && [ $FAIL -eq 0 ]; then
  echo COMPLETE $1

# If some PASS and some FAIL, then consider translation in progress
elif [ $PASS -gt 0 ] && [ $FAIL -gt 0 ]; then
  echo PARTIAL $1

# Otherwise, consider the translation a failure
else
  echo FAILED $1
fi
