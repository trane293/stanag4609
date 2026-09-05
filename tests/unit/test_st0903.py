from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from stanag4609.errors import ChecksumError, DecodeError
from stanag4609.klv.ber import encode_ber_length, encode_ber_oid
from stanag4609.klv.checksum import running_sum_16
from stanag4609.klv.model import KLVPacket
from stanag4609.st0903 import (
    VMTI_KEY,
    AlgorithmLocalSet,
    DetectionStatus,
    Location,
    OntologyLocalSet,
    RawVMTIValue,
    VMTIValidationContext,
    VObjectLocalSet,
    VTargetData,
    VTrackerLocalSet,
    decode_vmti_local_set,
    encode_algorithm_local_set,
    encode_location,
    encode_ontology_local_set,
    encode_vmti_local_set,
    encode_vtarget,
)
from stanag4609.st1204 import (
    MIISCoreIdentifier,
    decode_miis_core_identifier,
    encode_miis_core_identifier,
)

_MIIS_RAW = encode_miis_core_identifier(
    MIISCoreIdentifier(
        version=1,
        minor_id=UUID("01020304-0506-4708-890a-0b0c10111213"),
    )
)
_MIIS = decode_miis_core_identifier(_MIIS_RAW)


def _item(tag: int, value: bytes) -> bytes:
    return encode_ber_oid(tag) + encode_ber_length(len(value)) + value


def _target(target_id: int, items: bytes) -> bytes:
    value = encode_ber_oid(target_id) + items
    return encode_ber_length(len(value)) + value


def _series(*values: bytes) -> bytes:
    return b"".join(encode_ber_length(len(value)) + value for value in values)


def _algorithm(identifier: int = 3) -> AlgorithmLocalSet:
    return AlgorithmLocalSet(identifier, "detector", "1.0")


def _ontology(identifier: int = 12) -> OntologyLocalSet:
    return OntologyLocalSet(
        identifier,
        "https://example.org/objects.owl",
        "https://example.org/objects.owl#Truck",
        label="truck",
    )


def _embedded_value(*, target_id: int = 7, target_items: bytes | None = None) -> bytes:
    if target_items is None:
        target_items = b"".join(
            [
                _item(1, bytes.fromhex("064000")),
                _item(2, bytes.fromhex("064000")),
                _item(3, bytes.fromhex("064100")),
                _item(5, b"\x50"),
                _item(17, encode_location(Location(0, 0, 0))),
                _item(23, b"\x01"),
                _item(99, b"future"),
            ]
        )
    series = _target(target_id, target_items)
    return b"".join(
        [
            _item(2, bytes.fromhex("0003824430F6CE40")),
            _item(3, b"DSTO_ADSS_VMTI"),
            _item(4, b"\x06"),
            _item(5, b"\x01"),
            _item(6, b"\x01"),
            _item(8, bytes.fromhex("0780")),
            _item(9, bytes.fromhex("0438")),
            _item(11, bytes.fromhex("0640")),
            _item(12, bytes.fromhex("0500")),
            _item(13, _MIIS_RAW),
            _item(101, series),
        ]
    )


def _standalone_packet(value_without_checksum: bytes) -> bytes:
    value = value_without_checksum + _item(1, b"\x00\x00")
    packet = VMTI_KEY + encode_ber_length(len(value)) + value
    return packet[:-2] + running_sum_16(packet[:-2]).to_bytes(2, "big")


def _replace_target_series(value: bytes, series: bytes) -> bytes:
    original_items = _item(1, bytes.fromhex("064000")) + _item(2, bytes.fromhex("064000"))
    original_items += _item(3, bytes.fromhex("064100")) + _item(5, b"\x50")
    original_items += _item(17, encode_location(Location(0, 0, 0)))
    original_items += _item(23, b"\x01") + _item(99, b"future")
    return value.replace(_item(101, _target(7, original_items)), _item(101, series))


def test_decode_embedded_vmti_and_target_series_losslessly() -> None:
    raw = _embedded_value()
    vmti = decode_vmti_local_set(raw, standalone=False)
    assert vmti.standalone is False
    assert vmti.packet is None
    assert bytes(vmti.local_set) == raw
    assert vmti.value(2) == datetime(2001, 4, 19, 4, 25, 21, tzinfo=timezone.utc)
    assert vmti.value(3) == "DSTO_ADSS_VMTI"
    assert vmti.value(4) == 6
    assert vmti.value(8) == 1920
    assert vmti.value(11) == 12.5
    assert vmti.value(12) == 10.0
    assert vmti.value(13) == _MIIS
    assert len(vmti.targets) == 1
    target = vmti.targets[0]
    assert target.target_id == 7
    assert target.value(1) == 409_600
    assert target.value(5) == 80
    assert target.value(23) is DetectionStatus.ACTIVE_MOVING
    assert target.local_set.getone(99) is not None


def test_decode_standalone_vmti_requires_and_validates_checksum() -> None:
    packet = _standalone_packet(_embedded_value())
    vmti = decode_vmti_local_set(packet)
    assert vmti.standalone is True
    assert vmti.packet is not None
    assert bytes(vmti.packet) == packet

    corrupted = bytearray(packet)
    corrupted[30] ^= 1
    with pytest.raises(ChecksumError, match="mismatch"):
        decode_vmti_local_set(bytes(corrupted))


def test_embedded_vmti_must_omit_checksum() -> None:
    with pytest.raises(ChecksumError, match="omitted"):
        decode_vmti_local_set(_embedded_value() + _item(1, b"\x00\x00"), standalone=False)


def test_vmti_mandatory_items_and_timestamp_order_are_enforced() -> None:
    value = _embedded_value()
    with pytest.raises(DecodeError, match="first"):
        decode_vmti_local_set(_item(99, b"extension") + value, standalone=False)
    with pytest.raises(DecodeError, match="version"):
        decode_vmti_local_set(value.replace(_item(4, b"\x06"), b""), standalone=False)
    with pytest.raises(DecodeError, match="numTargetsReported"):
        decode_vmti_local_set(value.replace(_item(6, b"\x01"), b""), standalone=False)


def test_all_vmti_and_vtarget_items_are_singletons() -> None:
    value = _embedded_value()
    with pytest.raises(DecodeError, match="occurs twice"):
        decode_vmti_local_set(value + _item(4, b"\x06"), standalone=False)
    duplicated = _item(1, b"\x01") + _item(1, b"\x02")
    with pytest.raises(DecodeError, match=r"target 7.*occurs twice"):
        decode_vmti_local_set(_embedded_value(target_items=duplicated), standalone=False)


def test_vtarget_ids_are_unique_and_each_pack_has_a_tlv_item() -> None:
    value = _embedded_value()
    target_series = value[value.index(b"\x65") :]
    duplicate_series_value = target_series[2:] * 2
    duplicated = value[: value.index(b"\x65")] + _item(101, duplicate_series_value)
    with pytest.raises(DecodeError, match="targetId 7 occurs twice"):
        decode_vmti_local_set(duplicated, standalone=False)

    with pytest.raises(DecodeError, match="at least one"):
        decode_vmti_local_set(
            _embedded_value(target_items=b"").replace(_target(7, b""), b"\x01\x07"),
            standalone=False,
        )

    with pytest.raises(DecodeError, match="vTargetSeries must contain at least one"):
        decode_vmti_local_set(
            _item(4, b"\x06") + _item(6, b"\x00") + _item(101, b""),
            standalone=False,
        )


def test_reported_target_count_matches_series() -> None:
    value = _embedded_value().replace(_item(6, b"\x01"), _item(6, b"\x02"))
    with pytest.raises(DecodeError, match=r"reports 2.*contains 1"):
        decode_vmti_local_set(value, standalone=False)


@pytest.mark.parametrize("status", [DetectionStatus.ACTIVE_MOVING, DetectionStatus.ACTIVE_STOPPED])
def test_active_target_requires_a_centroid_representation(status: DetectionStatus) -> None:
    items = _item(5, b"\x50") + _item(23, bytes((status.value,)))
    with pytest.raises(DecodeError, match="centroid representation"):
        decode_vmti_local_set(_embedded_value(target_items=items), standalone=False)


def test_pixel_coordinate_pairs_and_target_ranges_are_validated() -> None:
    with pytest.raises(DecodeError, match="both be present"):
        decode_vmti_local_set(
            _embedded_value(target_items=_item(19, b"\x01") + _item(23, b"\x00")),
            standalone=False,
        )
    with pytest.raises(DecodeError, match="confidence"):
        decode_vmti_local_set(
            _embedded_value(target_items=_item(5, b"\x65") + _item(23, b"\x00")),
            standalone=False,
        )
    with pytest.raises(DecodeError, match="detectionStatus"):
        decode_vmti_local_set(_embedded_value(target_items=_item(23, b"\x05")), standalone=False)


def test_remaining_basic_vtarget_fields_are_typed() -> None:
    items = b"".join(
        [
            _item(4, b"\x1b"),
            _item(6, bytes.fromhex("0ACD")),
            _item(7, b"\x32"),
            _item(8, bytes.fromhex("DAA520")),
            _item(9, bytes.fromhex("3354")),
            _item(10, bytes.fromhex("3A6667")),
            _item(19, bytes.fromhex("0368")),
            _item(20, bytes.fromhex("0471")),
            _item(22, b"\x03"),
            _item(23, b"\x00"),
        ]
    )
    raw = _embedded_value(target_items=items) + _item(
        102, _series(encode_algorithm_local_set(_algorithm()))
    )
    target = decode_vmti_local_set(raw, standalone=False).targets[0]
    assert target.value(4) == 27
    assert target.value(6) == 2765
    assert target.value(7) == 50
    assert target.value(8) == (218, 165, 32)
    assert target.value(9) == 13_140
    assert float(target.value(10)) == pytest.approx(10, abs=0.01)
    assert target.value(19) == 872
    assert target.value(20) == 1137
    assert target.value(22) == 3
    assert target.value(999, "missing") == "missing"


def test_other_top_level_fields_and_defaults_are_exposed() -> None:
    algorithm = _algorithm()
    ontology = _ontology()
    raw = _embedded_value().replace(
        _item(3, b"DSTO_ADSS_VMTI"),
        _item(3, b"VMTI")
        + _item(10, b"EO Nose")
        + _item(102, _series(encode_algorithm_local_set(algorithm)))
        + _item(103, _series(encode_ontology_local_set(ontology)))
        + _item(99, b"future"),
    )
    vmti = decode_vmti_local_set(raw, standalone=False)
    assert vmti.value(10) == "EO Nose"
    assert vmti.value(102) == (algorithm,)
    assert vmti.value(103) == (ontology,)
    assert vmti.value(999, "missing") == "missing"
    assert vmti.local_set.getone(99) is not None


@pytest.mark.parametrize(
    "old, new, message",
    [
        (_item(4, b"\x06"), _item(4, b"\x00"), "vmtiLsVersionNum"),
        (_item(4, b"\x06"), _item(4, b"\x00\x06"), "minimal unsigned"),
        (_item(8, bytes.fromhex("0780")), _item(8, b"\x00"), "frameWidth"),
        (_item(11, bytes.fromhex("0640")), _item(11, bytes.fromhex("E000")), "special"),
        (_item(3, b"DSTO_ADSS_VMTI"), _item(3, b"\xff"), "UTF-8"),
        (_item(3, b"DSTO_ADSS_VMTI"), _item(3, b" VMTI"), "trimmed UTF-8"),
        (_item(3, b"DSTO_ADSS_VMTI"), _item(3, b"VMTI "), "trimmed UTF-8"),
        (_item(3, b"DSTO_ADSS_VMTI"), _item(3, b"VM\x01TI"), "UTF-8 control"),
    ],
)
def test_invalid_top_level_values_are_rejected(old: bytes, new: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_vmti_local_set(_embedded_value().replace(old, new), standalone=False)


def test_standalone_required_items_and_checksum_policy() -> None:
    raw = _embedded_value()
    missing_timestamp = raw.replace(_item(2, bytes.fromhex("0003824430F6CE40")), b"")
    with pytest.raises(DecodeError, match="precisionTimeStamp"):
        decode_vmti_local_set(_standalone_packet(missing_timestamp))
    missing_fov = raw.replace(_item(11, bytes.fromhex("0640")), b"")
    with pytest.raises(DecodeError, match="vmtiHorizontalFov"):
        decode_vmti_local_set(_standalone_packet(missing_fov))

    unchecked = bytearray(_standalone_packet(raw))
    unchecked[-1] ^= 1
    assert decode_vmti_local_set(bytes(unchecked), verify_checksum=False).standalone


def test_packet_input_key_and_resource_limit_are_validated() -> None:
    wrong = KLVPacket(b"\x06\x0e\x2b\x34" + bytes(12), _embedded_value(), b"\x7f")
    with pytest.raises(DecodeError, match="Universal Key"):
        decode_vmti_local_set(wrong)
    with pytest.raises(ValueError, match="max_targets"):
        decode_vmti_local_set(_embedded_value(), standalone=False, max_targets=0)
    two_targets = _target(7, _item(23, b"\x00")) + _target(8, _item(23, b"\x00"))
    oversized = _replace_target_series(_embedded_value(), two_targets).replace(
        _item(6, b"\x01"), _item(6, b"\x02")
    )
    with pytest.raises(DecodeError, match="configured maximum"):
        decode_vmti_local_set(oversized, standalone=False, max_targets=1)


@pytest.mark.parametrize(
    "series, message",
    [
        (b"\x81", "Pack length"),
        (b"\x05\x07\x17\x01\x00", "declares"),
        (b"\x02\x81\x80", "targetId"),
    ],
)
def test_truncated_target_series_are_rejected(series: bytes, message: str) -> None:
    raw = _replace_target_series(_embedded_value(), series)
    with pytest.raises(DecodeError, match=message):
        decode_vmti_local_set(raw, standalone=False)


def test_vtarget_rejects_nonminimal_variable_uint() -> None:
    raw = _embedded_value(target_items=_item(6, b"\x00\x01"))
    with pytest.raises(DecodeError, match="minimal unsigned"):
        decode_vmti_local_set(raw, standalone=False)


def test_encode_vtarget_uses_ber_oid_id_and_typed_pixel_fields() -> None:
    target = VTargetData(
        1234,
        {
            1: 409_600,
            4: 27,
            5: 80,
            6: 2765,
            7: 50,
            8: (218, 165, 32),
            9: 13_140,
            22: 3,
            23: DetectionStatus.ACTIVE_MOVING,
            99: RawVMTIValue(b"future"),
        },
    )
    encoded = encode_vtarget(target)
    pack_length, used = encoded[0], 1
    assert pack_length == len(encoded) - used
    assert encoded[used : used + 2] == bytes.fromhex("8952")

    vmti = decode_vmti_local_set(
        encode_vmti_local_set(
            {4: 6, 8: 1920},
            targets=(target,),
            algorithms=(_algorithm(),),
        ),
        standalone=False,
    )
    decoded = vmti.targets[0]
    assert decoded.target_id == 1234
    assert decoded.value(1) == 409_600
    assert decoded.value(4) == 27
    assert decoded.value(5) == 80
    assert decoded.value(6) == 2765
    assert decoded.value(7) == 50
    assert decoded.value(8) == (218, 165, 32)
    assert decoded.value(9) == 13_140
    assert decoded.value(22) == 3
    assert decoded.value(23) is DetectionStatus.ACTIVE_MOVING
    assert decoded.local_set.getone(99).value == b"future"


def test_vtarget_roundtrips_absolute_location_and_geospatial_contour() -> None:
    target_location = Location(49.2827, -123.1207, 112)
    contour = (
        Location(49.28, -123.13, 100),
        Location(49.29, -123.11, 125),
    )
    target = VTargetData(
        41,
        {
            17: target_location,
            18: contour,
            23: DetectionStatus.INACTIVE,
        },
    )
    vmti = decode_vmti_local_set(
        encode_vmti_local_set({4: 6}, targets=(target,), standalone=False),
        standalone=False,
    )

    decoded = vmti.targets[0]
    assert encode_location(decoded.value(17)) == encode_location(target_location)
    assert tuple(encode_location(value) for value in decoded.value(18)) == tuple(
        encode_location(value) for value in contour
    )


def test_vtarget_geospatial_fields_require_typed_values_and_boundary() -> None:
    with pytest.raises(TypeError, match="targetLocation requires Location"):
        encode_vtarget(VTargetData(1, {17: (0, 0, 0)}))
    with pytest.raises(TypeError, match="tuple of Location"):
        encode_vtarget(VTargetData(1, {18: [Location(0, 0, 0), Location(1, 1, 1)]}))
    with pytest.raises(ValueError, match="at least two"):
        encode_vtarget(VTargetData(1, {18: (Location(0, 0, 0),)}))


def test_vtarget_legacy_geospatial_fields_use_official_imap_mappings() -> None:
    target = VTargetData(
        42,
        {
            10: 10,
            11: 12,
            12: 10_000,
            13: 10,
            14: -10,
            15: 5,
            16: -5,
            23: DetectionStatus.INACTIVE,
        },
    )
    decoded = decode_vmti_local_set(
        encode_vmti_local_set({4: 6}, targets=(target,), standalone=False),
        standalone=False,
    ).targets[0]

    assert decoded.get(10).raw == bytes.fromhex("3A6667")
    assert decoded.get(11).raw == bytes.fromhex("3E6667")
    assert decoded.get(12).raw == bytes.fromhex("2A94")
    for tag, expected in ((10, 10), (11, 12), (12, 10_000), (13, 10), (14, -10), (15, 5), (16, -5)):
        assert float(decoded.value(tag)) == pytest.approx(expected, abs=0.01)


def test_vtarget_legacy_geospatial_fields_validate_values_and_specials() -> None:
    with pytest.raises(ValueError, match="targetLocationOffsetLat"):
        encode_vtarget(VTargetData(1, {10: 20}))
    with pytest.raises(TypeError, match="targetHae"):
        encode_vtarget(VTargetData(1, {12: "high"}))
    with pytest.raises(ValueError, match="must be finite"):
        encode_vtarget(VTargetData(1, {10: float("nan")}))
    with pytest.raises(DecodeError, match="requires 3 byte"):
        decode_vmti_local_set(
            _embedded_value(target_items=_item(10, bytes(2)) + _item(23, b"\x00")),
            standalone=False,
        )
    raw = _embedded_value(target_items=_item(10, b"\xd0\x00\x00") + _item(23, b"\x00"))
    with pytest.raises(DecodeError, match="does not permit IMAP special"):
        decode_vmti_local_set(raw, standalone=False)


def test_standalone_vtarget_forbids_parent_offsets_and_requires_active_location() -> None:
    parent_fields = {
        2: 987_654_321_000_000,
        4: 6,
        11: 12.5,
        12: 10.0,
        13: _MIIS,
    }
    offset_target = VTargetData(1, {10: 1, 23: DetectionStatus.INACTIVE})
    with pytest.raises(ValueError, match="offset Items 10-11 and 13-16"):
        encode_vmti_local_set(parent_fields, targets=(offset_target,), standalone=True)

    embedded = encode_vmti_local_set(parent_fields, targets=(offset_target,), standalone=False)
    with pytest.raises(DecodeError, match="offset Items 10-11 and 13-16"):
        decode_vmti_local_set(_standalone_packet(embedded))

    coasting = VTargetData(2, {23: DetectionStatus.ACTIVE_COASTING})
    with pytest.raises(ValueError, match="active target requires targetLocation"):
        encode_vmti_local_set(parent_fields, targets=(coasting,), standalone=True)


def test_vtarget_roundtrips_vtracker_and_validates_tracker_algorithm_reference() -> None:
    tracker = VTrackerLocalSet(
        track_id=UUID("f81d4fae-7dec-11d0-a765-00a0c91e6bf6"),
        track_history_series=(Location(49, -123, 10),),
        algorithm_id=3,
    )
    target = VTargetData(41, {23: DetectionStatus.INACTIVE, 104: tracker})
    vmti = decode_vmti_local_set(
        encode_vmti_local_set(
            {4: 6},
            targets=(target,),
            algorithms=(_algorithm(3),),
            standalone=False,
        ),
        standalone=False,
    )
    assert vmti.targets[0].value(104).track_id == tracker.track_id
    assert vmti.targets[0].value(104).algorithm_id == 3

    with pytest.raises(ValueError, match=r"VTracker algorithmId 3.*Algorithm Series"):
        encode_vmti_local_set({4: 6}, targets=(target,), standalone=False)
    with pytest.raises(TypeError, match="vTracker requires VTrackerLocalSet"):
        encode_vtarget(VTargetData(1, {104: RawVMTIValue(b"not typed")}))


def test_encode_embedded_vmti_for_ai_bounding_box() -> None:
    target = VTargetData(
        7,
        {
            1: 409_600,
            2: 409_600,
            3: 409_856,
            5: 93,
            19: 872,
            20: 1137,
            23: DetectionStatus.ACTIVE_MOVING,
        },
    )
    raw = encode_vmti_local_set(
        {
            2: 987_654_321_000_000,
            3: "TRUCK_DETECTOR",
            4: 6,
            5: 1,
            8: 1920,
            9: 1080,
            11: 12.5,
            12: 10.0,
            13: _MIIS,
        },
        targets=(target,),
        standalone=False,
    )
    vmti = decode_vmti_local_set(raw, standalone=False)
    assert vmti.value(2) == datetime(2001, 4, 19, 4, 25, 21, tzinfo=timezone.utc)
    assert vmti.value(6) == 1
    assert vmti.local_set.getone(11).value == bytes.fromhex("0640")
    assert vmti.local_set.getone(12).value == bytes.fromhex("0500")
    assert vmti.targets[0].value(5) == 93


def test_encode_standalone_vmti_owns_checksum() -> None:
    packet = encode_vmti_local_set(
        {
            2: 987_654_321_000_000,
            4: 6,
            11: 12.5,
            12: 10.0,
            13: _MIIS_RAW,
        },
        standalone=True,
    )
    vmti = decode_vmti_local_set(packet)
    assert vmti.standalone
    assert vmti.value(6) == 0
    assert running_sum_16(packet[:-2]) == int.from_bytes(packet[-2:], "big")


def test_vmti_encoder_validates_counts_reserved_items_and_target_values() -> None:
    target = VTargetData(7, {23: DetectionStatus.INACTIVE})
    with pytest.raises(ValueError, match="numTargetsReported"):
        encode_vmti_local_set({4: 6, 6: 2}, targets=(target,), standalone=False)
    with pytest.raises(ValueError, match="owned"):
        encode_vmti_local_set({1: 0, 4: 6}, standalone=False)
    with pytest.raises(ValueError, match="vmtiLsVersionNum"):
        encode_vmti_local_set({}, standalone=False)
    with pytest.raises(TypeError, match="RawVMTIValue"):
        encode_vmti_local_set({4: 6, 99: b"implicit raw"}, standalone=False)
    with pytest.raises(ValueError, match="confidence"):
        encode_vtarget(VTargetData(1, {5: 101, 23: DetectionStatus.INACTIVE}))
    with pytest.raises(ValueError, match="centroid representation"):
        encode_vtarget(VTargetData(1, {23: DetectionStatus.ACTIVE_MOVING}))


def test_vmti_encoder_accepts_datetime_text_raw_series_and_byte_color() -> None:
    timestamp = datetime(2001, 4, 19, 4, 25, 21, tzinfo=timezone.utc)
    target = VTargetData(
        2,
        {
            8: bytes.fromhex("DAA520"),
            19: 1,
            20: 2,
            23: DetectionStatus.ACTIVE_STOPPED,
            107: (VObjectLocalSet(12, 90.0),),
        },
    )
    raw = encode_vmti_local_set(
        {
            2: timestamp,
            3: "DETECTOR",
            4: 6,
            10: "EO Nose",
            11: 12.5,
        },
        targets=(target,),
        algorithms=(_algorithm(),),
        ontologies=(_ontology(),),
    )
    vmti = decode_vmti_local_set(raw, standalone=False)
    assert vmti.value(2) == timestamp
    assert vmti.value(3) == "DETECTOR"
    assert vmti.value(10) == "EO Nose"
    assert vmti.value(102) == (_algorithm(),)
    assert vmti.targets[0].value(8) == (218, 165, 32)
    assert vmti.targets[0].value(107)[0].ontology_id == 12


@pytest.mark.parametrize(
    "target, error, message",
    [
        (object(), TypeError, "VTargetData"),
        (VTargetData(1, {}), ValueError, "at least one"),
        (VTargetData(1, {99: b"raw"}), TypeError, "RawVMTIValue"),
        (VTargetData(1, {19: 1}), ValueError, "both be present"),
        (VTargetData(1, {23: 5}), ValueError, "detectionStatus"),
        (VTargetData(1, {8: (1, 2)}), TypeError, "three-int tuple"),
        (VTargetData(1, {8: (1, "2", 3)}), TypeError, "three integers"),
        (VTargetData(1, {8: (1, 256, 3)}), ValueError, "0..255"),
        (VTargetData(1, {8: b"xx"}), ValueError, "3 bytes"),
        (VTargetData(1, {4: True}), TypeError, "requires int"),
        (VTargetData(1, {1: 0}), ValueError, "targetCentroid"),
        (VTargetData(1, {256: RawVMTIValue(b"x")}), ValueError, "one-byte"),
    ],
)
def test_vtarget_encoder_rejects_ambiguous_or_invalid_inputs(
    target: object, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        encode_vtarget(target)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "values, error, message",
    [
        ({4: 6, 101: RawVMTIValue(b"series")}, ValueError, "owned"),
        ({2: True, 4: 6}, TypeError, "boolean"),
        ({2: datetime(2020, 1, 1), 4: 6}, ValueError, "timezone-aware"),
        ({2: -1, 4: 6}, ValueError, "64-bit"),
        ({3: "", 4: 6}, ValueError, "must not be empty"),
        ({3: " padded", 4: 6}, ValueError, "trimmed"),
        ({3: "bad\x01text", 4: 6}, ValueError, "control"),
        ({3: "x" * 33, 4: 6}, ValueError, "32"),
        ({4: 6, 11: True}, TypeError, "int, float, or Fraction"),
        ({4: 6, 11: 181}, ValueError, "between 0 and 180"),
        ({4: 6, 13: "miis"}, TypeError, "MIISCoreIdentifier or bytes"),
        ({4: 6, 13: b""}, ValueError, "not conformant with ST 1204"),
    ],
)
def test_vmti_encoder_rejects_invalid_top_level_values(
    values: dict[int, object], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        encode_vmti_local_set(values)


def test_vmti_encoder_requires_standalone_profile_items() -> None:
    with pytest.raises(ValueError, match="precisionTimeStamp"):
        encode_vmti_local_set({4: 6}, standalone=True)
    with pytest.raises(ValueError, match="vmtiHorizontalFov"):
        encode_vmti_local_set(
            {2: 987_654_321_000_000, 4: 6},
            standalone=True,
        )
    with pytest.raises(TypeError, match="RawVMTIValue data"):
        RawVMTIValue(bytearray(b"not immutable"))  # type: ignore[arg-type]


def test_pixel_number_targets_require_top_level_frame_width() -> None:
    target = VTargetData(1, {1: 1, 23: DetectionStatus.ACTIVE_MOVING})
    with pytest.raises(ValueError, match="frameWidth Item 8"):
        encode_vmti_local_set({4: 6}, targets=(target,))

    without_width = _embedded_value().replace(_item(8, bytes.fromhex("0780")), b"")
    with pytest.raises(DecodeError, match="frameWidth Item 8"):
        decode_vmti_local_set(without_width, standalone=False)

    row_column_only = VTargetData(
        1,
        {
            19: 10,
            20: 20,
            23: DetectionStatus.ACTIVE_MOVING,
        },
    )
    encoded = encode_vmti_local_set({4: 6}, targets=(row_column_only,))
    assert decode_vmti_local_set(encoded, standalone=False).value(8) is None


def test_vtarget_pixel_geometry_accepts_frame_edges() -> None:
    target = VTargetData(
        1,
        {
            1: 12,
            2: 1,
            3: 12,
            19: 3,
            20: 4,
            23: DetectionStatus.ACTIVE_MOVING,
        },
    )

    encoded = encode_vmti_local_set({4: 6, 8: 4, 9: 3}, targets=(target,))

    assert decode_vmti_local_set(encoded, standalone=False).targets[0].value(1) == 12


@pytest.mark.parametrize(
    "values, message",
    [
        ({1: 13, 23: DetectionStatus.INACTIVE}, "targetCentroid.*outside"),
        ({2: 13, 3: 13, 23: DetectionStatus.INACTIVE}, "boundingBoxTopLeft.*outside"),
        ({19: 4, 20: 1, 23: DetectionStatus.INACTIVE}, "centroidPixRow.*outside"),
        ({19: 1, 20: 5, 23: DetectionStatus.INACTIVE}, "centroidPixCol.*outside"),
        ({2: 6, 3: 5, 23: DetectionStatus.INACTIVE}, "top-left.*below or right"),
    ],
)
def test_vmti_encoder_rejects_target_pixel_geometry_outside_frame(
    values: dict[int, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        encode_vmti_local_set(
            {4: 6, 8: 4, 9: 3},
            targets=(VTargetData(1, values),),
        )


def test_vmti_decoder_rejects_target_pixel_geometry_outside_frame() -> None:
    invalid_target = encode_vtarget(
        VTargetData(7, {19: 1081, 20: 1, 23: DetectionStatus.INACTIVE})
    )
    raw = _replace_target_series(_embedded_value(), invalid_target)

    with pytest.raises(DecodeError, match=r"centroidPixRow.*outside"):
        decode_vmti_local_set(raw, standalone=False)


def test_vmti_context_enforces_parent_and_frame_timestamp_relationships() -> None:
    timestamp = 1_700_000_000_000_000
    differing = VMTIValidationContext(
        vmti_frame_timestamp=timestamp,
        parent_timestamp=timestamp - 1,
    )
    with pytest.raises(ValueError, match="parent and VMTI-MI frame timestamps differ"):
        encode_vmti_local_set({4: 6}, context=differing)

    without_timestamp = encode_vmti_local_set({4: 6})
    with pytest.raises(DecodeError, match="parent and VMTI-MI frame timestamps differ"):
        decode_vmti_local_set(
            without_timestamp,
            standalone=False,
            context=differing,
        )

    with pytest.raises(ValueError, match="must equal the VMTI-MI frame timestamp"):
        encode_vmti_local_set({2: timestamp + 1, 4: 6}, context=differing)
    matching = encode_vmti_local_set({2: timestamp, 4: 6}, context=differing)
    assert decode_vmti_local_set(matching, standalone=False, context=differing).value(2)

    same_time = VMTIValidationContext(
        vmti_frame_timestamp=timestamp,
        parent_timestamp=timestamp,
    )
    assert encode_vmti_local_set({4: 6}, context=same_time)


def test_vmti_context_enforces_two_frame_offset_boundary() -> None:
    timestamp = 1_700_000_000_000_000
    target = VTargetData(1, {10: 1, 23: DetectionStatus.INACTIVE})
    exact_boundary = VMTIValidationContext(
        vmti_frame_timestamp=timestamp + 2_000,
        parent_timestamp=timestamp,
        frame_period_microseconds=1_000,
    )
    encoded = encode_vmti_local_set(
        {2: timestamp + 2_000, 4: 6},
        targets=(target,),
        context=exact_boundary,
    )
    assert decode_vmti_local_set(
        encoded,
        standalone=False,
        context=exact_boundary,
    ).targets

    stale = VMTIValidationContext(
        vmti_frame_timestamp=timestamp + 2_001,
        parent_timestamp=timestamp,
        frame_period_microseconds=1_000,
    )
    with pytest.raises(ValueError, match="more than two frames"):
        encode_vmti_local_set(
            {2: timestamp + 2_001, 4: 6},
            targets=(target,),
            context=stale,
        )
    stale_wire = encode_vmti_local_set(
        {2: timestamp + 2_001, 4: 6},
        targets=(target,),
    )
    with pytest.raises(DecodeError, match="more than two frames"):
        decode_vmti_local_set(stale_wire, standalone=False, context=stale)


def test_vmti_context_enforces_different_image_source_fields() -> None:
    context = VMTIValidationContext(different_image_source=True)
    with pytest.raises(ValueError, match="vmtiHorizontalFov"):
        encode_vmti_local_set({4: 6}, context=context)
    with pytest.raises(ValueError, match="vmtiVerticalFov"):
        encode_vmti_local_set({4: 6, 11: 10}, context=context)
    with pytest.raises(ValueError, match="miisId"):
        encode_vmti_local_set({4: 6, 11: 10, 12: 5}, context=context)

    valid = encode_vmti_local_set(
        {4: 6, 11: 10, 12: 5, 13: _MIIS},
        context=context,
    )
    assert decode_vmti_local_set(valid, standalone=False, context=context)
    with pytest.raises(DecodeError, match="vmtiHorizontalFov"):
        decode_vmti_local_set(
            encode_vmti_local_set({4: 6}),
            standalone=False,
            context=context,
        )


def test_vmti_context_enforces_declared_frame_dimensions() -> None:
    target = VTargetData(1, {1: 1, 23: DetectionStatus.ACTIVE_MOVING})
    context = VMTIValidationContext(frame_width=1920, frame_height=1080)
    with pytest.raises(ValueError, match="frameWidth does not match"):
        encode_vmti_local_set(
            {4: 6, 8: 100, 9: 1080},
            targets=(target,),
            context=context,
        )
    wrong = encode_vmti_local_set({4: 6, 8: 100, 9: 1080}, targets=(target,))
    with pytest.raises(DecodeError, match="frameWidth does not match"):
        decode_vmti_local_set(wrong, standalone=False, context=context)


def test_vmti_context_enforces_total_target_count_when_subset_is_reported() -> None:
    target = VTargetData(1, {23: DetectionStatus.INACTIVE})
    culled = VMTIValidationContext(total_targets_detected=2)
    with pytest.raises(ValueError, match="totalNumTargetsDetected Item 5 is required"):
        encode_vmti_local_set({4: 6}, targets=(target,), context=culled)

    encoded = encode_vmti_local_set(
        {4: 6, 5: 2},
        targets=(target,),
        context=culled,
    )
    assert decode_vmti_local_set(encoded, standalone=False, context=culled).value(5) == 2

    with pytest.raises(ValueError, match="does not match"):
        encode_vmti_local_set(
            {4: 6, 5: 3},
            targets=(target,),
            context=culled,
        )
    wrong = encode_vmti_local_set({4: 6, 5: 3}, targets=(target,))
    with pytest.raises(DecodeError, match="does not match"):
        decode_vmti_local_set(wrong, standalone=False, context=culled)

    missing = encode_vmti_local_set({4: 6}, targets=(target,))
    with pytest.raises(DecodeError, match="totalNumTargetsDetected Item 5 is required"):
        decode_vmti_local_set(missing, standalone=False, context=culled)

    uncensored = VMTIValidationContext(total_targets_detected=1)
    assert encode_vmti_local_set({4: 6}, targets=(target,), context=uncensored)


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"vmti_frame_timestamp": True}, "cannot be boolean"),
        ({"parent_timestamp": datetime(2020, 1, 1)}, "timezone-aware"),
        ({"frame_period_microseconds": 0}, "must be positive"),
        ({"frame_period_microseconds": float("inf")}, "must be finite"),
        ({"frame_width": True}, "frame_width"),
        ({"total_targets_detected": -1}, "total_targets_detected"),
        ({"total_targets_detected": True}, "total_targets_detected"),
        ({"different_image_source": 1}, "different_image_source"),
    ],
)
def test_vmti_validation_context_rejects_ambiguous_facts(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        VMTIValidationContext(**kwargs)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="VMTIValidationContext"):
        encode_vmti_local_set({4: 6}, context=object())  # type: ignore[arg-type]


def test_vmti_miis_id_requires_and_decodes_st1204_core_identifier() -> None:
    encoded = encode_vmti_local_set({4: 6, 13: _MIIS}, standalone=False)
    assert decode_vmti_local_set(encoded, standalone=False).value(13) == _MIIS

    encoded_from_bytes = encode_vmti_local_set({4: 6, 13: _MIIS_RAW}, standalone=False)
    assert encoded_from_bytes == encoded
    malformed = _item(4, b"\x06") + _item(6, b"\x00") + _item(13, b"not-a-core-id")
    with pytest.raises(DecodeError, match="ST 1204 Core Identifier"):
        decode_vmti_local_set(malformed, standalone=False)
