from __future__ import annotations

import pytest

from stanag4609.errors import DecodeError
from stanag4609.imap import IMAPSpecialKind, IMAPSpecialValue
from stanag4609.klv.ber import encode_ber_length, encode_ber_oid
from stanag4609.st0903 import (
    AlgorithmLocalSet,
    OntologyEntityResolution,
    OntologyLocalSet,
    RawVMTIValue,
    VFeatureLocalSet,
    VMTIValidationContext,
    VObjectLocalSet,
    VTargetData,
    decode_algorithm_local_set,
    decode_ontology_local_set,
    decode_vmti_local_set,
    decode_vobject_local_set,
    encode_algorithm_local_set,
    encode_ontology_local_set,
    encode_vmti_local_set,
    encode_vobject_local_set,
)


class _OntologyResolver:
    def __init__(self, result: OntologyEntityResolution | None) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def resolve_entity(
        self,
        ontology_iri: str,
        entity_iri: str,
    ) -> OntologyEntityResolution | None:
        self.calls.append((ontology_iri, entity_iri))
        return self.result


def _algorithm(identifier: int = 7) -> AlgorithmLocalSet:
    return AlgorithmLocalSet(
        algorithm_id=identifier,
        name="YOLO vehicle detector",
        version="11.3.0",
        algorithm_class="detector",
        n_frames=1,
    )


def _ontology(identifier: int = 12) -> OntologyLocalSet:
    return OntologyLocalSet(
        ontology_id=identifier,
        ontology_iri="https://example.org/fmv-objects.owl",
        entity_iri="https://example.org/fmv-objects.owl#Truck",
        version_iri="https://example.org/fmv-objects/1.0",
        label="truck",
    )


def test_algorithm_local_set_all_fields_round_trip() -> None:
    value = _algorithm()
    wire = encode_algorithm_local_set(value)
    decoded = decode_algorithm_local_set(wire)
    assert decoded == value
    assert encode_algorithm_local_set(decoded, preserve=True) == wire


def test_ontology_local_set_all_fields_round_trip() -> None:
    value = _ontology()
    wire = encode_ontology_local_set(value)
    decoded = decode_ontology_local_set(wire)
    assert decoded == value
    assert encode_ontology_local_set(decoded, preserve=True) == wire


def test_ontology_resolver_validates_owl_entity_and_exact_label() -> None:
    ontology = _ontology()
    resolver = _OntologyResolver(
        OntologyEntityResolution(
            ontology_iri=ontology.ontology_iri,
            entity_iri=ontology.entity_iri,
            is_owl_ontology=True,
            rdfs_labels=frozenset({"truck", "lorry"}),
            skos_preferred_labels=frozenset({"cargo truck"}),
        )
    )
    context = VMTIValidationContext(ontology_resolver=resolver)

    wire = encode_vmti_local_set({4: 6}, ontologies=(ontology,), context=context)
    decoded = decode_vmti_local_set(wire, standalone=False, context=context)

    assert decoded.ontologies == (ontology,)
    assert resolver.calls == [
        (ontology.ontology_iri, ontology.entity_iri),
        (ontology.ontology_iri, ontology.entity_iri),
    ]


@pytest.mark.parametrize(
    "resolution,match",
    [
        (None, "does not contain entityIRI"),
        (
            OntologyEntityResolution(
                ontology_iri="https://example.org/fmv-objects.owl",
                entity_iri="https://example.org/fmv-objects.owl#Truck",
                is_owl_ontology=False,
            ),
            "does not reference an OWL ontology",
        ),
        (
            OntologyEntityResolution(
                ontology_iri="https://example.org/fmv-objects.owl",
                entity_iri="https://example.org/fmv-objects.owl#Truck",
                is_owl_ontology=True,
                rdfs_labels=frozenset({"Truck"}),
            ),
            "label 'truck' does not exactly match",
        ),
    ],
)
def test_ontology_resolver_rejects_nonconformant_semantics(
    resolution: OntologyEntityResolution | None,
    match: str,
) -> None:
    resolver = _OntologyResolver(resolution)
    with pytest.raises(ValueError, match=match):
        encode_vmti_local_set(
            {4: 6},
            ontologies=(_ontology(),),
            context=VMTIValidationContext(ontology_resolver=resolver),
        )


def test_ontology_resolver_failures_are_decode_errors() -> None:
    wire = encode_vmti_local_set({4: 6}, ontologies=(_ontology(),))
    resolver = _OntologyResolver(None)

    with pytest.raises(DecodeError, match="does not contain entityIRI"):
        decode_vmti_local_set(
            wire,
            standalone=False,
            context=VMTIValidationContext(ontology_resolver=resolver),
        )


def test_ontology_resolver_is_optional_and_resolution_is_validated() -> None:
    ontology = _ontology()
    encode_vmti_local_set({4: 6}, ontologies=(ontology,))

    with pytest.raises(TypeError, match="ontology_resolver"):
        VMTIValidationContext(ontology_resolver=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="resolved ontology_iri"):
        encode_vmti_local_set(
            {4: 6},
            ontologies=(ontology,),
            context=VMTIValidationContext(
                ontology_resolver=_OntologyResolver(
                    OntologyEntityResolution(
                        ontology_iri="https://example.org/other.owl",
                        entity_iri=ontology.entity_iri,
                        is_owl_ontology=True,
                    )
                )
            ),
        )


def test_vobject_and_feature_round_trip() -> None:
    value = VObjectLocalSet(
        ontology_id=12,
        confidence=96.5,
        confidence_length=2,
        features=(
            VFeatureLocalSet(
                ontology_id=13,
                confidence=75.0,
                confidence_length=1,
            ),
        ),
    )
    wire = encode_vobject_local_set(value)
    decoded = decode_vobject_local_set(wire)
    assert decoded.ontology_id == 12
    assert decoded.confidence == pytest.approx(96.5, abs=0.01)
    assert decoded.features[0].ontology_id == 13
    assert decoded.features[0].confidence == pytest.approx(75.0, abs=0.5)
    assert encode_vobject_local_set(decoded, preserve=True) == wire


def test_integrated_algorithm_ontology_and_target_object_series() -> None:
    algorithms = (_algorithm(),)
    ontologies = (_ontology(),)
    target = VTargetData(
        42,
        {
            1: 10,
            22: 7,
            107: (VObjectLocalSet(ontology_id=12, confidence=98.0),),
        },
    )
    wire = encode_vmti_local_set(
        {4: 6, 8: 100},
        targets=(target,),
        algorithms=algorithms,
        ontologies=ontologies,
    )
    decoded = decode_vmti_local_set(wire, standalone=False)
    assert decoded.algorithms == algorithms
    assert decoded.ontologies == ontologies
    objects = decoded.targets[0].value(107)
    assert objects[0].ontology_id == 12
    assert objects[0].confidence == pytest.approx(98.0, abs=0.5)
    assert decoded.value(102) == decoded.algorithms
    assert decoded.value(103) == decoded.ontologies


def test_multibyte_series_lengths_and_special_confidence_are_lossless() -> None:
    algorithm = AlgorithmLocalSet(1, "detector-" + "x" * 140, "1.0")
    special = IMAPSpecialValue(IMAPSpecialKind.POSITIVE_QUIET_NAN, b"\xd0\x00")
    target = VTargetData(1, {1: 1, 107: (VObjectLocalSet(2, special, (), 2),)})
    wire = encode_vmti_local_set(
        {4: 6, 8: 100},
        targets=(target,),
        algorithms=(algorithm,),
        ontologies=(
            OntologyLocalSet(
                2,
                "https://example.org/objects.owl",
                "https://example.org/objects.owl#Unknown",
            ),
        ),
    )
    assert b"\x81" in wire
    decoded = decode_vmti_local_set(wire, standalone=False)
    assert decoded.algorithms[0].name == algorithm.name
    assert decoded.targets[0].value(107)[0].confidence == special


def test_series_identifiers_and_references_are_validated() -> None:
    with pytest.raises(TypeError, match="targets"):
        encode_vmti_local_set({4: 6}, targets=(object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unique algorithm"):
        encode_vmti_local_set({4: 6}, algorithms=(_algorithm(), _algorithm()), ontologies=())
    with pytest.raises(ValueError, match="unique ontology"):
        encode_vmti_local_set({4: 6}, ontologies=(_ontology(), _ontology()), algorithms=())
    with pytest.raises(ValueError, match="algorithmId 7"):
        encode_vmti_local_set({4: 6}, targets=(VTargetData(1, {1: 1, 22: 7}),))
    with pytest.raises(ValueError, match="ontologyId 12"):
        encode_vmti_local_set(
            {4: 6},
            targets=(VTargetData(1, {1: 1, 107: (VObjectLocalSet(ontology_id=12),)}),),
        )
    with pytest.raises(ValueError, match="parentId 99"):
        encode_vmti_local_set(
            {4: 6},
            ontologies=(
                _ontology(),
                OntologyLocalSet(
                    13,
                    "https://example.org/fmv-objects.owl",
                    "https://example.org/fmv-objects.owl#Vehicle",
                    parent_id=99,
                ),
            ),
        )
    with pytest.raises(ValueError, match="VFeature ontologyId 13"):
        encode_vmti_local_set(
            {4: 6},
            targets=(
                VTargetData(
                    1,
                    {
                        1: 1,
                        107: (VObjectLocalSet(12, features=(VFeatureLocalSet(13),)),),
                    },
                ),
            ),
            ontologies=(_ontology(),),
        )


@pytest.mark.parametrize(
    "decoder,wire,required",
    [
        (decode_algorithm_local_set, b"\x01\x01\x01", "name"),
        (decode_ontology_local_set, b"\x01\x01\x01", "ontologyIRI"),
        (decode_vobject_local_set, b"\x04\x01\x00", "ontologyId"),
    ],
)
def test_nested_local_set_mandatory_items(decoder: object, wire: bytes, required: str) -> None:
    with pytest.raises(DecodeError, match=required):
        decoder(wire)  # type: ignore[operator]


def test_deprecated_items_are_preserved_but_not_canonically_generated() -> None:
    wire = b"\x01\x01x\x03\x01\x01"
    decoded = decode_vobject_local_set(wire)
    assert decoded.extensions[1].data == b"x"
    assert encode_vobject_local_set(decoded, preserve=True) == wire
    with pytest.raises(ValueError, match="after Item"):
        encode_vobject_local_set(decoded)


def test_duplicate_and_malformed_items_are_rejected() -> None:
    with pytest.raises(DecodeError, match="twice"):
        decode_algorithm_local_set(b"\x01\x01\x01\x01\x01\x02\x02\x01a\x03\x01v")
    with pytest.raises(DecodeError, match="at most 8"):
        decode_ontology_local_set(b"\x01\x09" + bytes(9) + b"\x03\x01x\x04\x01y")
    with pytest.raises(DecodeError, match="between 1 and 3"):
        decode_vobject_local_set(b"\x03\x01\x01\x04\x04" + bytes(4))
    with pytest.raises(DecodeError, match="one-byte UINT"):
        decode_algorithm_local_set(
            encode_ber_oid(128) + b"\x01x" + b"\x01\x01\x01\x02\x01a\x03\x01v"
        )


def test_nested_extensions_are_lossless_and_validated() -> None:
    algorithm = _algorithm()
    algorithm = AlgorithmLocalSet(
        algorithm.algorithm_id,
        algorithm.name,
        algorithm.version,
        extensions={9: RawVMTIValue(b"future")},
    )
    decoded = decode_algorithm_local_set(encode_algorithm_local_set(algorithm))
    assert decoded.extensions[9].data == b"future"
    with pytest.raises(ValueError, match="extension"):
        encode_algorithm_local_set(
            AlgorithmLocalSet(1, "name", "1", extensions={2: RawVMTIValue(b"x")})
        )
    with pytest.raises(TypeError, match="RawVMTIValue"):
        encode_algorithm_local_set(
            AlgorithmLocalSet(1, "name", "1", extensions={9: b"x"})  # type: ignore[dict-item]
        )


@pytest.mark.parametrize(
    "factory,match",
    [
        (lambda: AlgorithmLocalSet(-1, "name", "1"), "between"),
        (lambda: AlgorithmLocalSet(1, "", "1"), "empty"),
        (lambda: AlgorithmLocalSet(1, " name", "1"), "trimmed"),
        (lambda: AlgorithmLocalSet(1, "bad\x01name", "1"), "control"),
        (lambda: OntologyLocalSet(1, "relative", "https://e/x"), "absolute IRI"),
        (lambda: VObjectLocalSet(1, 101.0), "between 0 and 100"),
        (lambda: VObjectLocalSet(1, 10.0, confidence_length=4), "between 1 and 3"),
        (lambda: VObjectLocalSet(1, features=[]), "features"),
    ],
)
def test_nested_model_validation(factory: object, match: str) -> None:
    with pytest.raises((TypeError, ValueError), match=match):
        factory()  # type: ignore[operator]


def test_nested_codec_type_and_series_failures() -> None:
    with pytest.raises(TypeError, match="AlgorithmLocalSet"):
        encode_algorithm_local_set(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="OntologyLocalSet"):
        encode_ontology_local_set(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="VObjectLocalSet"):
        encode_vobject_local_set(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="data must be bytes"):
        decode_algorithm_local_set(bytearray())  # type: ignore[arg-type]
    with pytest.raises(DecodeError, match="canonical UTF-8"):
        decode_algorithm_local_set(b"\x01\x01\x01\x02\x01\xff\x03\x01v")
    malformed_series = b"\x04\x01\x06" + encode_ber_oid(102) + b"\x01\x00"
    malformed_series += b"\x06\x01\x00"
    with pytest.raises(DecodeError, match="mandatory algorithmId"):
        decode_vmti_local_set(malformed_series, standalone=False)


def test_dangling_reference_on_decode_is_rejected() -> None:
    target_value = b"\x01\x16\x01\x07"
    target = encode_ber_length(len(target_value)) + target_value
    wire = b"\x04\x01\x06\x06\x01\x01" + encode_ber_oid(101)
    wire += encode_ber_length(len(target)) + target
    with pytest.raises(DecodeError, match="algorithmId 7"):
        decode_vmti_local_set(wire, standalone=False)
