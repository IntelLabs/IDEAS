# 0 "/home/some/path.c"
# 0 "<built-in>"

extern int a; extern int b;
float c[7] = {1.0, 2.0,
3.0, 4.0, 5.0,
    6.0,
  7.0};

# 1 "some/other/path.c" 1 3 4

void foo() {
      int x = 10; int y = 20;
    // A comment
    int z = 20;


    /* A comment
    block */
    if (z > 15) { z += 5;    } else {
        z -= 5;
    }
}
