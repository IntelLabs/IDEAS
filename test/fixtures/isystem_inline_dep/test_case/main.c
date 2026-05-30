#include "alloc.h"

int do_work(int x);
void *my_calloc(size_t n, size_t sz);

int main(void) {
    void *p = my_calloc(4, sizeof(int));
    if (p) free(p);
    return do_work(42);
}
