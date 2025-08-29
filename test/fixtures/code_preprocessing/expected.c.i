int x;


int main() {
    char text[128];
    printf("Hello World!\n");

    while (fgets(text, 128,
                           stdin
                                )) {
        fputs(text,
                   stdout
                         );
    }

    return 0;
}
