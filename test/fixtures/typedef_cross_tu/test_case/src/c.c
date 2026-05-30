#include "types.h"

struct Y {
    X *member;
    int id;
};

struct Y *alloc_y(void) {
    return (struct Y *)0;
}
