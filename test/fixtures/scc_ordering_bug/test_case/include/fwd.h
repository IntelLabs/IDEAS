#ifndef FWD_H
#define FWD_H

struct vtable_t {
    int (*fn)(int);
};

extern struct vtable_t vtable;

/* Forward declaration only — no definition of helper here. */
int helper(int x);

#endif
