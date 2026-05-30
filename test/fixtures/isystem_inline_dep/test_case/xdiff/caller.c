#include "bridge.h"

typedef struct {
    int val;
} item_t;

static item_t *make_item(int val) {
    item_t *p;
    if (!(p = (item_t *)xdl_malloc(sizeof(item_t))))
        return (void *)0;
    p->val = val;
    return p;
}

int do_work(int x) {
    item_t *item = make_item(x);
    if (item) return item->val;
    return -1;
}
