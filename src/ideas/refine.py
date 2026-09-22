#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
from abc import ABC, abstractmethod
from typing import Self
from dataclasses import dataclass, field, replace
from collections.abc import Iterator, Sequence

import dspy
from dspy.utils.exceptions import AdapterParseError

from .model import format_usage


@dataclass(kw_only=True)
class Attempt:
    index: int = 0
    total: int = 0
    _rejected: bool | None = field(default=None, init=False)

    def accept(self) -> None:
        self._record(rejected=False)

    def reject(self) -> None:
        self._record(rejected=True)

    # Subclasses record why they judged as they did; the loop only needs to know whether
    def _record(self, *, rejected: bool) -> None:
        if self._rejected is not None:
            raise RuntimeError("Attempt was already judged!")
        self._rejected = rejected

    @property
    def success(self) -> bool:
        if self._rejected is None:
            raise RuntimeError(
                f"Attempt {self.index}/{self.total} ended without accept() or reject()!"
            )
        return not self._rejected


@dataclass(frozen=True)
class Feedback:
    review: str = ""
    build: str = ""
    scope: str = ""

    def __bool__(self) -> bool:
        return bool(self.review or self.build or self.scope)


@dataclass(kw_only=True)
class CodeAttempt(Attempt, ABC):
    pred: dspy.Prediction
    prior_feedback: Feedback
    rejection: Feedback = field(default=Feedback(), init=False)

    @property
    @abstractmethod
    def summary(self) -> str: ...

    def reject(self, *, build: Sequence[str] = (), scope: Sequence[str] = ()) -> None:
        if not build and not scope:
            raise ValueError("Attempt was rejected with no reason!")
        super().reject()
        self.rejection = Feedback(build=_bullets(build), scope=_bullets(scope))

    # Both channels at once, for readers that do not care which one complained
    @property
    def reason(self) -> str:
        return "\n".join(text for text in (self.rejection.build, self.rejection.scope) if text)

    # Only the session judges build and scope, so the review carries over untouched
    @property
    def next_feedback(self) -> Feedback:
        return replace(self.rejection, review=self.prior_feedback.review)

    @property
    def headline(self) -> str:
        counter = "" if self.success else f" ({self.index}/{self.total})"
        return f"{self.summary}{counter}: {format_usage(self.pred)}"

    # The headline alone is what gets logged; the sections below it are for the commit body
    @property
    def message(self) -> str:
        msg = self.headline
        for heading, text in (
            ("Reasoning", self.pred.get("reasoning", "")),
            ("Feedback", self.prior_feedback.review),
            ("Build Feedback", self.rejection.build),
            ("Scope Feedback", self.rejection.scope),
        ):
            if text:
                msg += f"\n\n# {heading}\n{text}"
        return msg


def _bullets(feedback: Sequence[str]) -> str:
    if len(feedback) == 1:
        return feedback[0]
    return "\n".join(f"- {f}" for f in feedback)


# `A` is what each iteration produces, `R` the candidate a cache hit can replay instead
class PredictSession[A: Attempt, R](ABC):
    @property
    @abstractmethod
    def _max_iters(self) -> int: ...

    @abstractmethod
    def _prime(self) -> R | None: ...

    @abstractmethod
    def _attempt(self, prior: A | None, replay: R | None) -> A: ...

    def __enter__(self) -> Self:
        return self

    def __iter__(self) -> Iterator[A]:
        replay: R | None = self._prime()

        # A replayed candidate spends the first iteration without generating anything, so
        # grant one more and leave as many real attempts as a cache miss would have had
        max_iters = self._max_iters + (1 if replay is not None else 0)

        prior: A | None = None
        for i in range(max_iters):
            # Replaying admits static candidates that violate safety, which the LLM then fixes
            try:
                attempt = self._attempt(prior, replay)
            # A malformed response is a failure a fresh attempt might survive
            except AdapterParseError:
                # The caller's own log line names what is being generated
                logging.getLogger(type(self).__module__).exception(
                    f"Generation failed on iteration {i + 1}/{max_iters}!"
                )
                # If this is the last iteration, raise
                if i == max_iters - 1:
                    raise
                # Otherwise attempt again before any logic
                continue
            finally:
                # Consumed once even when the attempt fails, so a retry generates instead
                replay = None

            attempt.index, attempt.total = i + 1, max_iters
            yield attempt

            if attempt.success:
                return

            prior = attempt

    def __exit__(self, *exc_info: object) -> None:
        pass
