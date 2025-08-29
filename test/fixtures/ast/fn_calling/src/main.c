#include "lib.h"
#include <stdio.h>
#include <math.h>


int double_value(int x) {
    printf("This printf call should be filtered!");
    return add(x, x);
}


int triple_value(int x) {
    return add(
        add(x, subtract(x, x)), add(x, x)
    );
}


int quadruple_value(int x) {
    return x * 4;
}


double double_absolute_value(double x) {
    printf("This and the below calls should be filtered!");
    return add(fabs(x), fabs(log10(pow(x, 10.0))));
}
