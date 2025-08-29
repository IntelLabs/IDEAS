#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import json
import logging


class CodePairFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        c_snippet = getattr(record, "c", None)
        rust_snippet = getattr(record, "rust", None)
        return c_snippet is not None and rust_snippet is not None


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        c_snippet = getattr(record, "c", None)
        rust_snippet = getattr(record, "rust", None)
        assert c_snippet is not None and rust_snippet is not None, (
            "Both C and Rust snippets must be present for JSON logging"
        )

        log_entry = {
            "c": c_snippet,
            "rust": rust_snippet,
        }
        return json.dumps(log_entry)
