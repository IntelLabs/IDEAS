#include "alloc.h"

void *my_calloc(size_t n, size_t sz) {
    void *p = my_alloc(n * sz);
    return p;
}
