#ifndef HEADER_H
#define HEADER_H

struct vtable_t {
    int (*fn)(int);
};

extern struct vtable_t vtable;

/* Full definition of helper — only included by state.c */
inline int helper(int x) {
    return vtable.fn(x);
}

#endif
