#include "lib.h"

const double PI = 3.141592;
float e_powers[4] = {1.0, 2.718281, 7.389056, 20.085536};

void set_point(struct Point *p, int x, int y) {
    p->x = x;
    p->y = y;
    return;
}
