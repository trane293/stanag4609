"""Run two detector branches, fuse them, and emit timed ST 0601/VMTI KLV."""

from __future__ import annotations

import asyncio

from stanag4609 import AlgorithmLocalSet, OntologyLocalSet
from stanag4609.sidecar import (
    Detection,
    FrameEnvelope,
    InferenceContext,
    InferenceOutput,
    InferenceStage,
    Parallel,
    PixelBoundingBox,
    Sequential,
    VMTIMetadataEmitter,
)


def vehicle_detector(_context: InferenceContext) -> InferenceOutput:
    return InferenceOutput(
        detections=(
            Detection(
                101,
                PixelBoundingBox(220, 140, 430, 330),
                0.96,
                label="truck",
                algorithm_id=1,
            ),
        )
    )


async def person_service(_context: InferenceContext) -> InferenceOutput:
    await asyncio.sleep(0)
    return InferenceOutput(
        detections=(
            Detection(
                202,
                PixelBoundingBox(700, 180, 760, 360),
                0.91,
                label="person",
                algorithm_id=2,
            ),
        )
    )


def fuse(context: InferenceContext) -> InferenceOutput:
    vehicles = context.result("vehicles")
    people = context.result("people")
    assert vehicles is not None and people is not None
    return InferenceOutput(detections=vehicles.detections + people.detections)


async def main() -> None:
    algorithms = (
        AlgorithmLocalSet(1, "vehicle-detector", "1.0", "detector", 1),
        AlgorithmLocalSet(2, "person-detector", "1.0", "detector", 1),
    )
    ontologies = (
        OntologyLocalSet(
            11,
            "https://example.org/fmv-objects.owl",
            "https://example.org/fmv-objects.owl#Truck",
            label="truck",
        ),
        OntologyLocalSet(
            12,
            "https://example.org/fmv-objects.owl",
            "https://example.org/fmv-objects.owl#Person",
            label="person",
        ),
    )
    graph = Sequential(
        Parallel(
            InferenceStage("vehicles", vehicle_detector, threaded=True),
            InferenceStage("people", person_service, timeout_seconds=0.2),
            max_concurrency=2,
        ),
        InferenceStage("fused", fuse),
    )
    frame = FrameEnvelope(
        sequence_number=300,
        pts=900_000,
        width=1_920,
        height=1_080,
        pixels=None,
        timestamp_microseconds=1_700_000_000_000_000,
        program_number=1,
        video_pid=0x101,
    )
    completed = await graph.run(InferenceContext(frame))
    timed_klv = VMTIMetadataEmitter(
        "fused",
        metadata_pid=0x120,
        metadata_service_id=7,
        leap_seconds=29,
        algorithms=algorithms,
        ontologies=ontologies,
        ontology_by_label={"truck": 11, "person": 12},
    )(completed)
    vmti = timed_klv.decoded.value(74)
    print(
        f"PID=0x{timed_klv.pid:04X} PTS={timed_klv.pts} "
        f"targets={[target.target_id for target in vmti.targets]} "
        f"KLV_bytes={len(timed_klv.packet.raw)}"
    )


if __name__ == "__main__":
    asyncio.run(main())
