"""ST 0903.6 VChip, Algorithm, Ontology, VObject, and VFeature Local Sets."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, TypeVar, runtime_checkable
from urllib.parse import urlsplit

from stanag4609.errors import DecodeError, NeedMoreData, TruncatedData
from stanag4609.imap import IMAPB, IMAPSpecialValue
from stanag4609.klv.ber import decode_ber_length, encode_ber_length
from stanag4609.klv.local_set import parse_local_set
from stanag4609.klv.model import LocalSet

_MAX_NESTED_TEXT_BYTES = 1_048_576
_MAX_SERIES_ITEMS = 100_000
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RawVMTIValue:
    """Explicit wire value for an unaudited VMTI extension item."""

    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("RawVMTIValue data must be bytes")


def _validate_uint(value: int, *, name: str, maximum: int = 2**64 - 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}")


def _validate_text(value: str, *, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")
    if value[0] in "\x00\t\n\r " or value[-1] in "\x00\t\n\r ":
        raise ValueError(f"{name} violates ST 0107 trimmed UTF-8 rules")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{name} violates ST 0107 UTF-8 control rules")
    if len(value.encode("utf-8")) > _MAX_NESTED_TEXT_BYTES:
        raise ValueError(f"{name} exceeds the configured text limit")


def _validate_iri(value: str, *, name: str) -> None:
    _validate_text(value, name=name)
    if any(character.isspace() for character in value) or not urlsplit(value).scheme:
        raise ValueError(f"{name} must be an absolute IRI")


@dataclass(frozen=True, slots=True)
class AlgorithmLocalSet:
    algorithm_id: int
    name: str
    version: str
    algorithm_class: str | None = None
    n_frames: int | None = None
    extensions: Mapping[int, RawVMTIValue] = field(default_factory=dict)
    local_set: LocalSet | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        _validate_uint(self.algorithm_id, name="ST 0903 algorithmId")
        _validate_text(self.name, name="ST 0903 Algorithm name")
        _validate_text(self.version, name="ST 0903 Algorithm version")
        if self.algorithm_class is not None:
            _validate_text(self.algorithm_class, name="ST 0903 Algorithm class")
        if self.n_frames is not None:
            _validate_uint(self.n_frames, name="ST 0903 Algorithm nFrames")


@dataclass(frozen=True, slots=True)
class OntologyLocalSet:
    ontology_id: int
    ontology_iri: str
    entity_iri: str
    parent_id: int | None = None
    version_iri: str | None = None
    label: str | None = None
    extensions: Mapping[int, RawVMTIValue] = field(default_factory=dict)
    local_set: LocalSet | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        _validate_uint(self.ontology_id, name="ST 0903 ontologyId")
        _validate_iri(self.ontology_iri, name="ST 0903 ontologyIRI")
        _validate_iri(self.entity_iri, name="ST 0903 entityIRI")
        if self.parent_id is not None:
            _validate_uint(self.parent_id, name="ST 0903 parentId")
        if self.version_iri is not None:
            _validate_iri(self.version_iri, name="ST 0903 versionIRI")
        if self.label is not None:
            _validate_text(self.label, name="ST 0903 Ontology label")


@dataclass(frozen=True, slots=True)
class OntologyEntityResolution:
    """Resolver evidence for one ST 0903 ontology/entity pair.

    Label values are their exact lexical forms. Language selection and network
    policy belong to the resolver implementation, not the KLV codec.
    """

    ontology_iri: str
    entity_iri: str
    is_owl_ontology: bool
    rdfs_labels: frozenset[str] = field(default_factory=frozenset)
    skos_preferred_labels: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _validate_iri(self.ontology_iri, name="resolved ontology_iri")
        _validate_iri(self.entity_iri, name="resolved entity_iri")
        if not isinstance(self.is_owl_ontology, bool):
            raise TypeError("is_owl_ontology must be boolean")
        for name, labels in (
            ("rdfs_labels", self.rdfs_labels),
            ("skos_preferred_labels", self.skos_preferred_labels),
        ):
            if not isinstance(labels, frozenset) or any(
                not isinstance(label, str) for label in labels
            ):
                raise TypeError(f"{name} must be a frozenset of strings")
            for label in labels:
                _validate_text(label, name=name)


@runtime_checkable
class OntologyResolver(Protocol):
    """Application-owned resolver used for optional ST 0903 semantic checks."""

    def resolve_entity(
        self,
        ontology_iri: str,
        entity_iri: str,
    ) -> OntologyEntityResolution | None:
        """Return evidence for an entity, or ``None`` when it cannot be resolved."""


def validate_ontology_semantics(
    ontologies: Iterable[OntologyLocalSet],
    resolver: OntologyResolver,
) -> None:
    """Validate OWL membership and exact optional labels using ``resolver``.

    This function performs no network access. A resolver may use an in-memory
    map, local OWL files, a cache, a database, or an application-controlled
    service. Every supplied ontology is resolved exactly once.
    """

    if not isinstance(resolver, OntologyResolver):
        raise TypeError("ontology_resolver must implement resolve_entity")
    for ontology in ontologies:
        if not isinstance(ontology, OntologyLocalSet):
            raise TypeError("ontologies must contain only OntologyLocalSet values")
        resolution = resolver.resolve_entity(ontology.ontology_iri, ontology.entity_iri)
        if resolution is None:
            raise ValueError(
                f"ST 0903 ontologyIRI {ontology.ontology_iri!r} does not contain "
                f"entityIRI {ontology.entity_iri!r}"
            )
        if not isinstance(resolution, OntologyEntityResolution):
            raise TypeError(
                "ontology_resolver must return OntologyEntityResolution or None"
            )
        if resolution.ontology_iri != ontology.ontology_iri:
            raise ValueError(
                "resolved ontology_iri does not exactly match the requested ontologyIRI"
            )
        if resolution.entity_iri != ontology.entity_iri:
            raise ValueError(
                "resolved entity_iri does not exactly match the requested entityIRI"
            )
        if not resolution.is_owl_ontology:
            raise ValueError(
                f"ST 0903 ontologyIRI {ontology.ontology_iri!r} does not reference "
                "an OWL ontology"
            )
        labels = resolution.rdfs_labels | resolution.skos_preferred_labels
        if ontology.label is not None and ontology.label not in labels:
            raise ValueError(
                f"ST 0903 label {ontology.label!r} does not exactly match an "
                "rdfs:label or skos:prefLabel"
            )


@dataclass(frozen=True, slots=True)
class VFeatureLocalSet:
    ontology_id: int
    confidence: float | IMAPSpecialValue | None = None
    confidence_length: int = 1
    extensions: Mapping[int, RawVMTIValue] = field(default_factory=dict)
    local_set: LocalSet | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        _validate_uint(self.ontology_id, name="ST 0903 VFeature ontologyId")
        _validate_confidence(self.confidence, self.confidence_length, name="VFeature")


@dataclass(frozen=True, slots=True)
class VObjectLocalSet:
    ontology_id: int
    confidence: float | IMAPSpecialValue | None = None
    features: tuple[VFeatureLocalSet, ...] = ()
    confidence_length: int = 1
    extensions: Mapping[int, RawVMTIValue] = field(default_factory=dict)
    local_set: LocalSet | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        _validate_uint(self.ontology_id, name="ST 0903 VObject ontologyId")
        _validate_confidence(self.confidence, self.confidence_length, name="VObject")
        if not isinstance(self.features, tuple) or any(
            not isinstance(item, VFeatureLocalSet) for item in self.features
        ):
            raise TypeError("ST 0903 VObject features must be VFeatureLocalSet values")


@dataclass(frozen=True, slots=True)
class VChipLocalSet:
    """Still-image chip embedded in or referenced by a VTarget."""

    image_type: str
    image_iri: str | None = None
    embedded_image: bytes | None = None
    extensions: Mapping[int, RawVMTIValue] = field(default_factory=dict)
    local_set: LocalSet | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        _validate_text(self.image_type, name="ST 0903 VChip imageType")
        if self.image_type not in {"jpeg", "png"}:
            raise ValueError("ST 0903 VChip imageType must be either jpeg or png")
        if self.image_iri is not None:
            _validate_iri(self.image_iri, name="ST 0903 VChip imageIRI")
        if self.embedded_image is not None and not isinstance(self.embedded_image, bytes):
            raise TypeError("ST 0903 VChip embeddedImage must be bytes or None")
        if self.embedded_image is not None:
            signature = _JPEG_SIGNATURE if self.image_type == "jpeg" else _PNG_SIGNATURE
            if not self.embedded_image.startswith(signature):
                raise ValueError(
                    "ST 0903 VChip embeddedImage does not match imageType "
                    f"{self.image_type!r}"
                )


@dataclass(frozen=True, slots=True)
class PixelRun:
    """One row-major starting pixel and run length in a VMask bit-mask series."""

    start_pixel: int
    run_length: int

    def __post_init__(self) -> None:
        _validate_uint(self.start_pixel, name="ST 0903 VMask start_pixel", maximum=2**48 - 1)
        _validate_uint(self.run_length, name="ST 0903 VMask run_length")
        if self.start_pixel == 0:
            raise ValueError("ST 0903 VMask start_pixel must be positive")
        if self.run_length == 0:
            raise ValueError("ST 0903 VMask run_length must be positive")

    @property
    def end_pixel(self) -> int:
        """Inclusive row-major pixel at the end of this run."""
        return self.start_pixel + self.run_length - 1


@dataclass(frozen=True, slots=True)
class VMaskLocalSet:
    """Pixel contour and/or run-length target segmentation mask."""

    pixel_contour: tuple[int, ...] = ()
    bit_mask_series: tuple[PixelRun, ...] = ()
    extensions: Mapping[int, RawVMTIValue] = field(default_factory=dict)
    local_set: LocalSet | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.pixel_contour, tuple) or any(
            isinstance(pixel, bool) or not isinstance(pixel, int)
            for pixel in self.pixel_contour
        ):
            raise TypeError("ST 0903 VMask pixelContour must contain integer pixels")
        if self.pixel_contour and len(self.pixel_contour) < 3:
            raise ValueError("ST 0903 VMask pixelContour requires at least three points")
        if any(not 1 <= pixel <= 2**48 - 1 for pixel in self.pixel_contour):
            raise ValueError("ST 0903 VMask pixelContour pixels must be positive V6 integers")
        if not isinstance(self.bit_mask_series, tuple) or any(
            not isinstance(run, PixelRun) for run in self.bit_mask_series
        ):
            raise TypeError("ST 0903 VMask bitMaskSeries must contain PixelRun values")
        if not self.pixel_contour and not self.bit_mask_series:
            raise ValueError("ST 0903 VMask requires a contour or bit-mask representation")

    def is_clockwise(self, frame_width: int) -> bool:
        """Return whether the contour is clockwise in top-left-origin image coordinates."""
        _validate_frame_dimension(frame_width, name="frame_width")
        if not self.pixel_contour:
            return False
        coordinates = tuple(
            ((pixel - 1) % frame_width + 1, (pixel - 1) // frame_width + 1)
            for pixel in self.pixel_contour
        )
        twice_area = sum(
            column * next_row - next_column * row
            for (column, row), (next_column, next_row) in zip(
                coordinates,
                (*coordinates[1:], coordinates[0]),
                strict=True,
            )
        )
        return twice_area > 0

    def validate_for_frame(self, frame_width: int, frame_height: int) -> None:
        """Validate pixel bounds and clockwise contour order for parent dimensions."""
        _validate_frame_dimension(frame_width, name="frame_width")
        _validate_frame_dimension(frame_height, name="frame_height")
        pixel_count = frame_width * frame_height
        if any(pixel > pixel_count for pixel in self.pixel_contour):
            raise ValueError("ST 0903 VMask contour exceeds the frame pixel count")
        if any(run.end_pixel > pixel_count for run in self.bit_mask_series):
            raise ValueError("ST 0903 VMask run exceeds the frame pixel count")
        if self.pixel_contour and not self.is_clockwise(frame_width):
            raise ValueError("ST 0903 VMask pixelContour must be clockwise")


def _validate_frame_dimension(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _validate_confidence(value: float | IMAPSpecialValue | None, length: int, *, name: str) -> None:
    if isinstance(length, bool) or not isinstance(length, int) or not 1 <= length <= 3:
        raise ValueError(f"ST 0903 {name} confidence_length must be between 1 and 3")
    if value is None or isinstance(value, IMAPSpecialValue):
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"ST 0903 {name} confidence must be numeric")
    if not 0 <= float(value) <= 100:
        raise ValueError(f"ST 0903 {name} confidence must be between 0 and 100")


def _item(tag: int, value: bytes) -> bytes:
    return bytes((tag,)) + encode_ber_length(len(value)) + value


def _uint(value: int, *, name: str) -> bytes:
    _validate_uint(value, name=name)
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")


def _decode_uint(value: bytes, *, name: str) -> int:
    if not 1 <= len(value) <= 8:
        raise DecodeError(f"ST 0903 {name} must contain at most 8 bytes")
    if len(value) > 1 and value[0] == 0:
        raise DecodeError(f"ST 0903 {name} must use minimal unsigned encoding")
    return int.from_bytes(value, "big")


def _text(value: str, *, name: str) -> bytes:
    _validate_text(value, name=name)
    return value.encode("utf-8")


def _decode_text(value: bytes, *, name: str) -> str:
    if not value or len(value) > _MAX_NESTED_TEXT_BYTES:
        raise DecodeError(f"ST 0903 {name} has an invalid text length")
    try:
        decoded = value.decode("utf-8")
        _validate_text(decoded, name=name)
    except (UnicodeDecodeError, ValueError) as error:
        raise DecodeError(f"ST 0903 {name} is not canonical UTF-8 text") from error
    return decoded


def _parse_nested(data: bytes, *, name: str, known: frozenset[int]) -> LocalSet:
    if not isinstance(data, bytes):
        raise TypeError(f"ST 0903 {name} data must be bytes")
    local_set = parse_local_set(data)
    seen: set[int] = set()
    for item in local_set.items:
        if len(item.tag_octets) != 1:
            raise DecodeError(f"ST 0903 {name} requires one-byte UINT tags")
        if item.tag in seen:
            raise DecodeError(f"ST 0903 {name} Item {item.tag} occurs twice")
        seen.add(item.tag)
    return local_set


def _extensions(local_set: LocalSet, known: frozenset[int]) -> dict[int, RawVMTIValue]:
    return {item.tag: RawVMTIValue(item.value) for item in local_set.items if item.tag not in known}


def _encode_extensions(extensions: Mapping[int, RawVMTIValue], *, after: int) -> bytes:
    output = bytearray()
    for tag in sorted(extensions):
        if isinstance(tag, bool) or not isinstance(tag, int):
            raise TypeError("ST 0903 nested extension tags must be integers")
        if not after < tag <= 255:
            raise ValueError(f"ST 0903 nested extension tag must be after Item {after}")
        value = extensions[tag]
        if not isinstance(value, RawVMTIValue):
            raise TypeError(f"ST 0903 nested extension Item {tag} requires RawVMTIValue")
        output.extend(_item(tag, value.data))
    return bytes(output)


def decode_algorithm_local_set(data: bytes) -> AlgorithmLocalSet:
    known = frozenset(range(1, 6))
    local_set = _parse_nested(data, name="Algorithm", known=known)
    items = {item.tag: item for item in local_set.items}
    missing = [
        name for tag, name in ((1, "algorithmId"), (2, "name"), (3, "version")) if tag not in items
    ]
    if missing:
        raise DecodeError(f"ST 0903 Algorithm is missing mandatory {', '.join(missing)}")
    try:
        return AlgorithmLocalSet(
            _decode_uint(items[1].value, name="algorithmId"),
            _decode_text(items[2].value, name="Algorithm name"),
            _decode_text(items[3].value, name="Algorithm version"),
            _decode_text(items[4].value, name="Algorithm class") if 4 in items else None,
            _decode_uint(items[5].value, name="nFrames") if 5 in items else None,
            _extensions(local_set, known),
            local_set,
        )
    except (TypeError, ValueError) as error:
        raise DecodeError(str(error)) from error


def encode_algorithm_local_set(value: AlgorithmLocalSet, *, preserve: bool = False) -> bytes:
    if not isinstance(value, AlgorithmLocalSet):
        raise TypeError("value must be an AlgorithmLocalSet")
    if preserve and value.local_set is not None:
        return value.local_set.raw
    output = bytearray()
    output.extend(_item(1, _uint(value.algorithm_id, name="algorithmId")))
    output.extend(_item(2, _text(value.name, name="Algorithm name")))
    output.extend(_item(3, _text(value.version, name="Algorithm version")))
    if value.algorithm_class is not None:
        output.extend(_item(4, _text(value.algorithm_class, name="Algorithm class")))
    if value.n_frames is not None:
        output.extend(_item(5, _uint(value.n_frames, name="nFrames")))
    output.extend(_encode_extensions(value.extensions, after=5))
    return bytes(output)


def decode_ontology_local_set(data: bytes) -> OntologyLocalSet:
    known = frozenset(range(1, 7))
    local_set = _parse_nested(data, name="Ontology", known=known)
    items = {item.tag: item for item in local_set.items}
    missing = [
        name
        for tag, name in ((1, "ontologyId"), (3, "ontologyIRI"), (4, "entityIRI"))
        if tag not in items
    ]
    if missing:
        raise DecodeError(f"ST 0903 Ontology is missing mandatory {', '.join(missing)}")
    try:
        return OntologyLocalSet(
            _decode_uint(items[1].value, name="ontologyId"),
            _decode_text(items[3].value, name="ontologyIRI"),
            _decode_text(items[4].value, name="entityIRI"),
            _decode_uint(items[2].value, name="parentId") if 2 in items else None,
            _decode_text(items[5].value, name="versionIRI") if 5 in items else None,
            _decode_text(items[6].value, name="Ontology label") if 6 in items else None,
            _extensions(local_set, known),
            local_set,
        )
    except (TypeError, ValueError) as error:
        raise DecodeError(str(error)) from error


def encode_ontology_local_set(value: OntologyLocalSet, *, preserve: bool = False) -> bytes:
    if not isinstance(value, OntologyLocalSet):
        raise TypeError("value must be an OntologyLocalSet")
    if preserve and value.local_set is not None:
        return value.local_set.raw
    output = bytearray(_item(1, _uint(value.ontology_id, name="ontologyId")))
    if value.parent_id is not None:
        output.extend(_item(2, _uint(value.parent_id, name="parentId")))
    output.extend(_item(3, _text(value.ontology_iri, name="ontologyIRI")))
    output.extend(_item(4, _text(value.entity_iri, name="entityIRI")))
    if value.version_iri is not None:
        output.extend(_item(5, _text(value.version_iri, name="versionIRI")))
    if value.label is not None:
        output.extend(_item(6, _text(value.label, name="Ontology label")))
    output.extend(_encode_extensions(value.extensions, after=6))
    return bytes(output)


def decode_vchip_local_set(data: bytes) -> VChipLocalSet:
    """Decode one embedded ST 0903.6 VChip Local Set."""
    known = frozenset({1, 2, 3})
    local_set = _parse_nested(data, name="VChip", known=known)
    items = {item.tag: item for item in local_set.items}
    if 1 not in items:
        raise DecodeError("ST 0903 VChip is missing mandatory imageType")
    try:
        return VChipLocalSet(
            _decode_text(items[1].value, name="VChip imageType"),
            _decode_text(items[2].value, name="VChip imageIRI") if 2 in items else None,
            items[3].value if 3 in items else None,
            _extensions(local_set, known),
            local_set,
        )
    except (TypeError, ValueError) as error:
        raise DecodeError(str(error)) from error


def encode_vchip_local_set(value: VChipLocalSet, *, preserve: bool = False) -> bytes:
    """Encode one embedded ST 0903.6 VChip Local Set."""
    if not isinstance(value, VChipLocalSet):
        raise TypeError("value must be a VChipLocalSet")
    if preserve and value.local_set is not None:
        return value.local_set.raw
    output = bytearray(_item(1, _text(value.image_type, name="VChip imageType")))
    if value.image_iri is not None:
        output.extend(_item(2, _text(value.image_iri, name="VChip imageIRI")))
    if value.embedded_image is not None:
        output.extend(_item(3, value.embedded_image))
    output.extend(_encode_extensions(value.extensions, after=3))
    return bytes(output)


def _decode_positive_v6(data: bytes, *, name: str) -> int:
    if not 1 <= len(data) <= 6:
        raise DecodeError(f"ST 0903 {name} must contain between 1 and 6 bytes")
    if len(data) > 1 and data[0] == 0:
        raise DecodeError(f"ST 0903 {name} must use minimal unsigned encoding")
    value = int.from_bytes(data, "big")
    if value == 0:
        raise DecodeError(f"ST 0903 {name} must be positive")
    return value


def _decode_pixel_contour(data: bytes) -> tuple[int, ...]:
    pixels: list[int] = []
    cursor = 0
    while cursor < len(data):
        try:
            length, used = decode_ber_length(data, cursor, max_value=6)
        except NeedMoreData as error:
            raise TruncatedData("truncated ST 0903 VMask pixelContour length") from error
        cursor += used
        end = cursor + length
        if end > len(data):
            raise TruncatedData("truncated ST 0903 VMask pixelContour pixel")
        pixels.append(_decode_positive_v6(data[cursor:end], name="VMask pixelContour pixel"))
        cursor = end
    if len(pixels) < 3:
        raise DecodeError("ST 0903 VMask pixelContour requires at least three points")
    return tuple(pixels)


def _encode_pixel_contour(pixels: tuple[int, ...]) -> bytes:
    output = bytearray()
    for pixel in pixels:
        encoded = _uint(pixel, name="VMask pixelContour pixel")
        output.extend(encode_ber_length(len(encoded)))
        output.extend(encoded)
    return bytes(output)


def _decode_pixel_run(data: bytes) -> PixelRun:
    try:
        pixel_length, used = decode_ber_length(data, 0, max_value=6)
    except NeedMoreData as error:
        raise TruncatedData("truncated ST 0903 VMask pixel-run start length") from error
    pixel_end = used + pixel_length
    if pixel_end > len(data):
        raise TruncatedData("truncated ST 0903 VMask pixel-run start pixel")
    start_pixel = _decode_positive_v6(
        data[used:pixel_end], name="VMask pixel-run start_pixel"
    )
    try:
        run_length, run_used = decode_ber_length(data, pixel_end)
    except NeedMoreData as error:
        raise TruncatedData("truncated ST 0903 VMask pixel-run run_length") from error
    if pixel_end + run_used != len(data):
        raise DecodeError("ST 0903 VMask pixel-run requires exactly one BER run length")
    try:
        return PixelRun(start_pixel, run_length)
    except (TypeError, ValueError) as error:
        raise DecodeError(str(error)) from error


def _encode_pixel_run(value: PixelRun) -> bytes:
    encoded_pixel = _uint(value.start_pixel, name="VMask pixel-run start_pixel")
    return (
        encode_ber_length(len(encoded_pixel))
        + encoded_pixel
        + encode_ber_length(value.run_length)
    )


def decode_vmask_local_set(data: bytes) -> VMaskLocalSet:
    """Decode one embedded ST 0903.6 VMask Local Set."""
    known = frozenset({1, 2})
    local_set = _parse_nested(data, name="VMask", known=known)
    items = {item.tag: item for item in local_set.items}
    try:
        return VMaskLocalSet(
            _decode_pixel_contour(items[1].value) if 1 in items else (),
            _decode_series(items[2].value, _decode_pixel_run, name="VMask pixel-run")
            if 2 in items
            else (),
            _extensions(local_set, known),
            local_set,
        )
    except (TypeError, ValueError) as error:
        raise DecodeError(str(error)) from error


def encode_vmask_local_set(value: VMaskLocalSet, *, preserve: bool = False) -> bytes:
    """Encode one embedded ST 0903.6 VMask Local Set."""
    if not isinstance(value, VMaskLocalSet):
        raise TypeError("value must be a VMaskLocalSet")
    if preserve and value.local_set is not None:
        return value.local_set.raw
    output = bytearray()
    if value.pixel_contour:
        output.extend(_item(1, _encode_pixel_contour(value.pixel_contour)))
    if value.bit_mask_series:
        output.extend(_item(2, _encode_series(value.bit_mask_series, _encode_pixel_run)))
    output.extend(_encode_extensions(value.extensions, after=2))
    return bytes(output)


def _decode_classification(data: bytes, *, feature: bool) -> VFeatureLocalSet | VObjectLocalSet:
    name = "VFeature" if feature else "VObject"
    known = frozenset({3, 4} if feature else {3, 4, 5})
    local_set = _parse_nested(data, name=name, known=known)
    items = {item.tag: item for item in local_set.items}
    if 3 not in items:
        raise DecodeError(f"ST 0903 {name} is missing mandatory ontologyId")
    confidence = None
    confidence_length = 1
    if 4 in items:
        confidence_length = len(items[4].value)
        if not 1 <= confidence_length <= 3:
            raise DecodeError(f"ST 0903 {name} confidence must contain between 1 and 3 bytes")
        confidence = IMAPB(0, 100, confidence_length).decode(items[4].value)
    identifier = _decode_uint(items[3].value, name=f"{name} ontologyId")
    extensions = _extensions(local_set, known)
    if feature:
        return VFeatureLocalSet(identifier, confidence, confidence_length, extensions, local_set)
    features = (
        _decode_series(items[5].value, decode_vfeature_local_set, name="VFeature")
        if 5 in items
        else ()
    )
    return VObjectLocalSet(
        identifier,
        confidence,
        features,
        confidence_length,
        extensions,
        local_set,
    )


def decode_vfeature_local_set(data: bytes) -> VFeatureLocalSet:
    return _decode_classification(data, feature=True)  # type: ignore[return-value]


def decode_vobject_local_set(data: bytes) -> VObjectLocalSet:
    return _decode_classification(data, feature=False)  # type: ignore[return-value]


def _encode_classification(
    value: VFeatureLocalSet | VObjectLocalSet,
    *,
    name: str,
    preserve: bool,
) -> bytes:
    if preserve and value.local_set is not None:
        return value.local_set.raw
    output = bytearray(_item(3, _uint(value.ontology_id, name=f"{name} ontologyId")))
    if value.confidence is not None:
        output.extend(_item(4, IMAPB(0, 100, value.confidence_length).encode(value.confidence)))
    after = 4
    if isinstance(value, VObjectLocalSet) and value.features:
        output.extend(_item(5, _encode_series(value.features, encode_vfeature_local_set)))
        after = 5
    output.extend(_encode_extensions(value.extensions, after=after))
    return bytes(output)


def encode_vfeature_local_set(value: VFeatureLocalSet, *, preserve: bool = False) -> bytes:
    if not isinstance(value, VFeatureLocalSet):
        raise TypeError("value must be a VFeatureLocalSet")
    return _encode_classification(value, name="VFeature", preserve=preserve)


def encode_vobject_local_set(value: VObjectLocalSet, *, preserve: bool = False) -> bytes:
    if not isinstance(value, VObjectLocalSet):
        raise TypeError("value must be a VObjectLocalSet")
    return _encode_classification(value, name="VObject", preserve=preserve)


def _decode_series(data: bytes, decoder: Callable[[bytes], T], *, name: str) -> tuple[T, ...]:
    values: list[T] = []
    cursor = 0
    while cursor < len(data):
        if len(values) >= _MAX_SERIES_ITEMS:
            raise DecodeError(f"ST 0903 {name} Series exceeds configured item limit")
        try:
            length, used = decode_ber_length(data, cursor, max_value=len(data) - cursor)
        except NeedMoreData as error:
            raise TruncatedData(f"truncated ST 0903 {name} Series length") from error
        cursor += used
        end = cursor + length
        if end > len(data):
            raise TruncatedData(f"truncated ST 0903 {name} Local Set")
        values.append(decoder(data[cursor:end]))
        cursor = end
    if not values:
        raise DecodeError(f"ST 0903 {name} Series must contain at least one Local Set")
    return tuple(values)


def _encode_series(values: tuple[T, ...], encoder: Callable[[T], bytes]) -> bytes:
    if not values:
        raise ValueError("ST 0903 Series must contain at least one Local Set")
    output = bytearray()
    for value in values:
        encoded = encoder(value)
        output.extend(encode_ber_length(len(encoded)))
        output.extend(encoded)
    return bytes(output)


def _decode_algorithm_series(data: bytes) -> tuple[AlgorithmLocalSet, ...]:
    return _decode_series(data, decode_algorithm_local_set, name="Algorithm")


def _decode_ontology_series(data: bytes) -> tuple[OntologyLocalSet, ...]:
    return _decode_series(data, decode_ontology_local_set, name="Ontology")


def _decode_vobject_series(data: bytes) -> tuple[VObjectLocalSet, ...]:
    return _decode_series(data, decode_vobject_local_set, name="VObject")


def _decode_vchip_series(data: bytes) -> tuple[VChipLocalSet, ...]:
    return _decode_series(data, decode_vchip_local_set, name="VChip")


def _encode_algorithm_series(values: tuple[AlgorithmLocalSet, ...]) -> bytes:
    return _encode_series(values, encode_algorithm_local_set)


def _encode_ontology_series(values: tuple[OntologyLocalSet, ...]) -> bytes:
    return _encode_series(values, encode_ontology_local_set)


def _encode_vobject_series(values: tuple[VObjectLocalSet, ...]) -> bytes:
    return _encode_series(values, encode_vobject_local_set)


def _encode_vchip_series(values: tuple[VChipLocalSet, ...]) -> bytes:
    return _encode_series(values, encode_vchip_local_set)
