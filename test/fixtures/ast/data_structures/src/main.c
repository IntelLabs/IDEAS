#include "lib.h"
#include <stdio.h>

enum Color circle_color;

struct Point p1;

int num_dimensions = 2;

const double half_pi = PI / 2.0;
const double half_pi;
static double one_third_pi = PI / 3.0;
static const double quarter_pi = PI / 4.0;

extern void set_point(struct Point *p, int x, int y);

int main(void) {
    struct Point p2;

    set_point(&p2, 10, 20);

    union Polar p3;

    p3.x = 30;
    p3.y = 40;
    p3.r = 50.0;

    double area = PI * p1.x * p2.y;
    double circumference = TWO_PI * p3.r;

    circle_color = red;
    if (circle_color == red) {
        printf("Circle is red\n");
        printf("Third power of e: %f\n", e_powers[2]);
    }

    if (circle_color == green)
        printf("Circle is green\n");

    return area / circumference;
}
