"""Optional ONNX Runtime session adapter for sidecar inference graphs."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeAlias

from stanag4609.sidecar.model import InferenceContext, InferenceOutput

OnnxInputBuilder: TypeAlias = Callable[[InferenceContext], Mapping[str, Any]]
OnnxOutputDecoder: TypeAlias = Callable[
    [tuple[Any, ...], InferenceContext], InferenceOutput
]


class OnnxRuntimeAdapter:
    """Adapt an ONNX Runtime ``InferenceSession`` without importing the runtime.

    ONNX defines tensor transport, not an object-detection result schema.
    Callers therefore provide explicit preprocessing and output-decoding hooks.
    The adapter validates session input names and the model-neutral result while
    keeping NumPy and ONNX Runtime out of the core dependency set.
    """

    __slots__ = (
        "input_builder",
        "output_decoder",
        "output_names",
        "session",
        "validate_input_names",
    )

    def __init__(
        self,
        session: Any,
        *,
        input_builder: OnnxInputBuilder,
        output_decoder: OnnxOutputDecoder,
        output_names: Sequence[str] | None = None,
        validate_input_names: bool = True,
    ) -> None:
        if not callable(getattr(session, "run", None)):
            raise TypeError("ONNX Runtime session must expose a callable run method")
        if not callable(input_builder):
            raise TypeError("input_builder must be callable")
        if not callable(output_decoder):
            raise TypeError("output_decoder must be callable")
        if output_names is not None and (
            isinstance(output_names, (str, bytes))
            or not isinstance(output_names, Sequence)
            or any(not isinstance(name, str) or not name for name in output_names)
        ):
            raise TypeError("output_names must be a sequence of non-empty strings or None")
        if not isinstance(validate_input_names, bool):
            raise TypeError("validate_input_names must be bool")
        if validate_input_names and not callable(getattr(session, "get_inputs", None)):
            raise TypeError(
                "validated ONNX Runtime session must expose a callable get_inputs method"
            )
        self.session = session
        self.input_builder = input_builder
        self.output_decoder = output_decoder
        self.output_names = None if output_names is None else tuple(output_names)
        self.validate_input_names = validate_input_names

    def __call__(self, context: InferenceContext) -> InferenceOutput:
        if not isinstance(context, InferenceContext):
            raise TypeError("context must be InferenceContext")
        inputs = self.input_builder(context)
        if not isinstance(inputs, Mapping) or any(
            not isinstance(name, str) or not name for name in inputs
        ):
            raise TypeError("input_builder must return a mapping with non-empty string keys")
        input_feed = dict(inputs)
        if self.validate_input_names:
            expected_names: set[str] = set()
            for item in self.session.get_inputs():
                name = getattr(item, "name", None)
                if not isinstance(name, str) or not name:
                    raise TypeError("ONNX Runtime input metadata must expose non-empty names")
                expected_names.add(name)
            actual_names = set(input_feed)
            missing = sorted(expected_names - actual_names)
            unexpected = sorted(actual_names - expected_names)
            if missing or unexpected:
                parts = []
                if missing:
                    parts.append(f"missing inputs {missing}")
                if unexpected:
                    parts.append(f"unexpected inputs {unexpected}")
                raise ValueError(
                    "ONNX Runtime input feed does not match session: " + "; ".join(parts)
                )

        output_names = None if self.output_names is None else list(self.output_names)
        values = self.session.run(output_names, input_feed)
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise TypeError("ONNX Runtime session.run must return a sequence")
        output = self.output_decoder(tuple(values), context)
        if not isinstance(output, InferenceOutput):
            raise TypeError("output_decoder must return InferenceOutput")
        return output
