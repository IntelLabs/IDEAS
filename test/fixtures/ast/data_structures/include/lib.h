#define TWO_PI 6.283185

extern int num_dimensions;

typedef double radius_t;

struct { int a; int b; } anonymous_struct;

struct Point {
    int x;
    int y;
};

union Polar {
    int x;
    int y;
    radius_t r;
};

enum Color {
    red = 1,
    green = 10,
    undefined = -1,
};

extern const double PI;
extern float e_powers[4];

extern void set_point(struct Point *p, int x, int y);
