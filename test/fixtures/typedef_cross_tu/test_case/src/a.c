#include "types.h"

struct X {
    X *self;
    int val;
};

X *create_x(int v) {
    (void)v;
    return (X *)0;
}

int main(void) {
    X *x = create_x(42);
    (void)x;
    return 0;
}
