int add_without_definition(int a, int b);

void print_message(const char* msg);

int add(int a, int b) {
    // This is a helpful comment that should not be stripped!
    return a + b;
}

void print_message(const char* msg) {
    printf("%s\\n", msg);
}

static int helper(int x) {
    return x * 2;
}
