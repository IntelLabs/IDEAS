//
// Copyright (C) 2025 Intel Corporation
//
// SPDX-License-Identifier: Apache-2.0
//

#include <stdio.h>
#include "constants.h"

int main(void) {
    for (int i = 0; i < BORDER_LENGTH; i++) {
        printf("%c", BORDER_CHAR);
    }

    printf("\n%s\n", GREETING);

    for (int i = 0; i < BORDER_LENGTH; i++) {
        printf("%c", BORDER_CHAR);
    }

    return 0;
}
