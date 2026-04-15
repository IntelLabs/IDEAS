#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import sys
import logging
from typing import TextIO
from rich.text import Text

from rich.console import Console

from kiss.core.print_to_console import ConsolePrinter


class ConsoleTee:
    """
    Duck-typed file-like that writes to both a real stream and a logger.
    The real stream gets raw output (preserving ANSI).
    The logger only receives complete lines (ANSI-stripped by the handler formatter).
    """

    class StripANSIFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            return Text.from_ansi(super().format(record)).plain

    def __init__(self, stream: TextIO, logger: logging.Logger, level: int = logging.INFO):
        self.stream = stream
        self.logger = logger
        self.level = level
        self.buf = ""
        self.encoding: str = getattr(stream, "encoding", "utf-8")

    def write(self, s: str) -> int:
        self.stream.write(s)
        self.buf += s
        # Write one line at a time
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line:
                self.logger.log(self.level, line)
        return len(s)

    def flush(self) -> None:
        self.stream.flush()

    def close(self) -> None:
        if self.buf:
            self.logger.log(self.level, self.buf)
            self.buf = ""

    def fileno(self) -> int:
        return self.stream.fileno()

    def isatty(self) -> bool:
        return hasattr(self.stream, "isatty") and self.stream.isatty()


class LoggingConsolePrinter(ConsolePrinter):
    def __init__(self, logger: logging.Logger, level: int = logging.INFO):
        tee = ConsoleTee(sys.__stdout__ or sys.stdout, logger, level)
        super().__init__(file=tee)
        self._console = Console(highlight=False, file=tee)  # type: ignore[reportArgumentType]
