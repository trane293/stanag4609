"""Encode one model detection as an ontology-backed ST 0903 VMTI value."""

from stanag4609 import AlgorithmLocalSet, OntologyLocalSet, decode_vmti_local_set
from stanag4609.sidecar import (
    Detection,
    FrameEnvelope,
    PixelBoundingBox,
    encode_embedded_vmti,
)


def main() -> None:
    frame = FrameEnvelope(
        sequence_number=1,
        pts=90_000,
        width=1920,
        height=1080,
        pixels=None,
        timestamp_microseconds=1_700_000_000_000_000,
    )
    detections = (
        Detection(
            target_id=42,
            bounding_box=PixelBoundingBox(100, 200, 301, 401),
            confidence=0.965,
            label="truck",
            algorithm_id=7,
        ),
    )
    algorithms = (AlgorithmLocalSet(7, "example-vehicle-detector", "1.0", "detector", 1),)
    ontologies = (
        OntologyLocalSet(
            ontology_id=12,
            ontology_iri="https://example.org/fmv-objects.owl",
            entity_iri="https://example.org/fmv-objects.owl#Truck",
            label="truck",
        ),
    )
    embedded = encode_embedded_vmti(
        frame,
        detections,
        system_name="example-sidecar",
        algorithms=algorithms,
        ontologies=ontologies,
        ontology_by_label={"truck": 12},
        leap_seconds=29,
    )
    decoded = decode_vmti_local_set(embedded, standalone=False)
    target = decoded.targets[0]
    print(target.target_id, decoded.ontologies[0].label, target.value(107)[0].confidence)


if __name__ == "__main__":
    main()
