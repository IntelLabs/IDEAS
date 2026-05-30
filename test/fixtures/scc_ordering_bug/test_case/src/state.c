#include "header.h"

int compute(int x);

/* Provide external definition of helper for callers that only see the declaration */
extern inline int helper(int x);

struct vtable_t vtable = { .fn = compute };
