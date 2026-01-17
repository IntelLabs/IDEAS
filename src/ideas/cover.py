#
# Copyright (C) 2023 Stanford Future Data Systems
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
from typing import Any, Callable

from litellm.exceptions import ContextWindowExceededError

import dspy
from dspy.predict.react import _fmt_exc

logger = logging.getLogger(__name__)


class CoVeR(dspy.Module):
    def __init__(
        self,
        signature: dspy.SignatureMeta,
        tools: list[dspy.Tool | Callable],
        success: str = "Success!",
        max_iters: int = 5,
        use_raw_fixer_output: bool = True,
    ):
        super().__init__()
        self.signature = signature = dspy.ensure_signature(signature)  # type: ignore
        self.success = success
        self.max_iters = max_iters
        self.use_raw_fixer_output = use_raw_fixer_output

        if len(tools) == 0:
            raise ValueError("Need at least one valid dspy.Tool!")
        dspy_tools = [t if isinstance(t, dspy.Tool) else dspy.Tool(t) for t in tools]
        tool_dict = {tool.name: tool for tool in dspy_tools}

        inputs = ", ".join([f"`{k}`" for k in signature.input_fields.keys()])
        outputs = ", ".join([f"`{k}`" for k in signature.output_fields.keys()])

        instr = [f"{signature.instructions}\n"] if signature.instructions else []
        instr.extend(
            [
                f"You are an Agent. In each episode, you will be given the fields {inputs} as input. And you can see your past trajectory so far.",
                f"Your goal is to use the supplied tools to collect any necessary information for producing {outputs}.\n",
                "After each tool call, you receive a resulting observation, which gets appended to your trajectory.\n",
                "When writing next_thought, you may reason about the current situation and plan for future steps.",
                "The tools are:\n",
            ]
        )
        for idx, tool in enumerate(tool_dict.values()):
            instr.append(f"({idx + 1}) {tool}")

        # Extract all task (non-wrapper) output names
        self.task_outputs: list[str] = list(signature.output_fields.keys())

        # Route each task output to its tools based on the name
        self.tools = tool_dict
        self.tool_args: dict[str, list[str]] = dict()
        for name, tool in self.tools.items():
            assert name, f"Tool name for {tool} is empty!"
            if not name == tool.name:
                raise ValueError(f"Tool name mismatch {name} != {tool.name}!")

            assert tool.args, f"Tool {tool} does not have any input arguments!"
            self.tool_args[name] = [
                output for output in self.task_outputs if output in tool.args
            ]

            if len(self.tool_args[name]) == 0:
                raise ValueError(
                    f"No valid outputs in {self.task_outputs} can be routed to tool {name}!"
                )

        self.cover_signature = (
            dspy.Signature(
                {**signature.input_fields, **signature.output_fields},  # type: ignore
                "\n".join(instr),
            )
            .append("trajectory", dspy.InputField(), type_=str)
            .append("next_thought", dspy.OutputField(), type_=str)
        )

        self.cover = dspy.Predict(self.cover_signature)

    def _format_trajectory(self, trajectory: dict[str, Any]):
        adapter = dspy.settings.adapter or dspy.ChatAdapter()
        trajectory_signature = dspy.Signature(f"{', '.join(trajectory.keys())} -> x")  # type: ignore
        return adapter.format_user_message_content(trajectory_signature, trajectory)  # type: ignore

    def forward(self, **input_args):
        trajectory = {}
        max_iters = input_args.pop("max_iters", self.max_iters)
        prediction = None
        for idx in range(max_iters):
            try:
                pred = self._call_with_potential_trajectory_truncation(
                    self.cover, trajectory, **input_args
                )
            except ValueError as err:
                logger.warning(
                    f"Ending the trajectory: Agent failed to select a valid tool: {_fmt_exc(err)}"
                )
                break

            assert pred is not None, "Prediction should not be None!"

            trajectory[f"thought_{idx}"] = pred.next_thought  # type: ignore
            tool_names, tool_args, observations = [], [], []
            for name in self.tools.keys():
                local_args = {arg: getattr(pred, arg) for arg in self.tool_args[name]}  # type: ignore
                tool_names.append(name)
                tool_args.append(str(local_args))
                try:
                    feedback = self.tools[name].func(**local_args)
                except Exception as err:
                    feedback = f"Execution error in {name}: {_fmt_exc(err)}"

                observations.append(feedback)

            prediction = dspy.Prediction(trajectory=trajectory, **pred)
            if self.success in observations:
                if self.use_raw_fixer_output:
                    # Return exactly the output that sucessfully satisfies the tools
                    return prediction
                break

            # Concatenate all tool calls, args used, and feedback in this step
            trajectory[f"tool_name_{idx}"] = "\n\n".join(tool_names)
            trajectory[f"tool_args_{idx}"] = "\n\n".join(tool_args)
            trajectory[f"observation_{idx}"] = "\n\n".join(observations)

        # Return the last failed prediction if max_iters reached
        if prediction is None:
            raise RuntimeError("No prediction was made during the forward pass!")
        return prediction

    def _call_with_potential_trajectory_truncation(
        self, module, trajectory, retries: int = 3, **input_args
    ):
        for _ in range(retries):
            try:
                return module(
                    **input_args,
                    trajectory=self._format_trajectory(trajectory),
                )
            except ContextWindowExceededError:
                logger.warning(
                    "Trajectory exceeded the context window, truncating the oldest tool call information."
                )
                trajectory = self.truncate_trajectory(trajectory)

        logger.warning(f"Unable to extract a prediction after {retries} retries!")
        return dict()

    def truncate_trajectory(self, trajectory):
        """Truncates the trajectory so that it fits in the context window.

        Users can override this method to implement their own truncation logic.
        """
        keys = list(trajectory.keys())
        if len(keys) < 4:
            # Every tool call has 4 keys: thought, tool_name, tool_args, and observation.
            raise ValueError(
                "The trajectory is too long so your prompt exceeded the context window, but the trajectory cannot be "
                "truncated because it only has one tool call."
            )

        for key in keys[:4]:
            trajectory.pop(key)

        return trajectory
