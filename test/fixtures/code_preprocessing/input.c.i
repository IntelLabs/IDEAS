# 1 "/usr/include/stdc-predef.h" 1 3 4
# 0 "<command-line>" 2
# 1 "/some/path/to/keep.c"
# 24 "/other/path/to/keep.c"
# 1 "/usr/include/stdio.h" 1 3 4
# 885 "/usr/include/stdio.h" 3 4
extern int __uflow (FILE *);
extern int __overflow (FILE *, int);
# 902 "/usr/include/stdio.h" 3 4

# 29 "/usr/include/x86_64-linux-gnu/bits/types.h" 2 3 4


typedef unsigned char __u_char;
typedef unsigned short int __u_short;
typedef unsigned int __u_int;
typedef unsigned long int __u_long;


# 25 "/home/path/to/keep.c" 2
int x;


# 26 "/home/path/to/main.c"
int main() {
    char text[128];
    printf("Hello World!\n");

    while (fgets(text, 128,
# 30 "/home/path/to/main.c" 3 4
                           stdin
# 30 "/home/path/to/main.c"
                                )) {
        fputs(text,
# 31 "/home/path/to/main.c" 3 4
                   stdout
# 31 "/home/path/to/main.c"
                         );
    }

    return 0;
}
