#ifndef ALLOC_H
#define ALLOC_H

#include <stdlib.h>

static inline void *my_alloc(size_t len) {
    return malloc(len);
}

#endif
