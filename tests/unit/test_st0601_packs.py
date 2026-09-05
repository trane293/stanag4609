from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from stanag4609.errors import DecodeError
from stanag4609.st0601 import (
    ActivePayloads,
    ActiveWavelengthList,
    AirbaseLocation,
    AirbaseLocations,
    ControlCommand,
    ControlCommandVerificationList,
    CountryCodes,
    ImageHorizonPixelPack,
    MetadataSubstreamID,
    PayloadList,
    PayloadRecord,
    SensorFrameRatePack,
    SpecialValue,
    ViewDomain,
    ViewDomainPair,
    WavelengthRecord,
    WavelengthsList,
    WaypointInfo,
    WaypointList,
    WaypointRecord,
    WeaponsStores,
    WeaponStatus,
    WeaponStore,
    decode_active_payloads,
    decode_active_wavelength_list,
    decode_airbase_locations,
    decode_control_command,
    decode_control_command_verification_list,
    decode_country_codes,
    decode_image_horizon_pixel_pack,
    decode_metadata_substream_id,
    decode_payload_list,
    decode_sensor_frame_rate_pack,
    decode_uas_local_set,
    decode_view_domain,
    decode_wavelengths_list,
    decode_waypoint_list,
    decode_weapons_stores,
    encode_active_payloads,
    encode_active_wavelength_list,
    encode_airbase_locations,
    encode_control_command,
    encode_control_command_verification_list,
    encode_country_codes,
    encode_field_value,
    encode_image_horizon_pixel_pack,
    encode_metadata_substream_id,
    encode_payload_list,
    encode_sensor_frame_rate_pack,
    encode_uas_local_set,
    encode_view_domain,
    encode_wavelengths_list,
    encode_waypoint_list,
    encode_weapons_stores,
    update_uas_local_set,
)


def test_image_horizon_official_minimum_vector() -> None:
    raw = bytes.fromhex("00 24 38 00")
    pack = decode_image_horizon_pixel_pack(raw)
    assert pack == ImageHorizonPixelPack(0, 36, 56, 0)
    assert encode_image_horizon_pixel_pack(pack) == raw


def test_image_horizon_full_geodetic_vector() -> None:
    raw = bytes.fromhex(
        "00 0A 14 00 8F695262 765457F2 F101A229 14BC082B"
    )
    pack = decode_image_horizon_pixel_pack(raw)
    assert pack.start_latitude == pytest.approx(-79.16385005189285)
    assert pack.start_longitude == pytest.approx(166.40081296041646)
    assert pack.end_latitude == pytest.approx(-10.542388633146132)
    assert pack.end_longitude == pytest.approx(29.157890122923014)
    assert encode_image_horizon_pixel_pack(pack) == raw


def test_image_horizon_truncation_and_error_indicator_are_distinct() -> None:
    latitude_only = ImageHorizonPixelPack(10, 0, 0, 20, start_latitude=-34.3)
    raw = encode_image_horizon_pixel_pack(latitude_only)
    assert len(raw) == 8
    assert decode_image_horizon_pixel_pack(raw).start_latitude == pytest.approx(-34.3)
    assert decode_image_horizon_pixel_pack(raw).start_longitude is None

    with_error = ImageHorizonPixelPack(
        10,
        0,
        0,
        20,
        start_latitude=SpecialValue.ERROR,
        start_longitude=143.2,
    )
    error_raw = encode_image_horizon_pixel_pack(with_error)
    assert error_raw[4:8] == bytes.fromhex("80000000")
    assert decode_image_horizon_pixel_pack(error_raw).start_latitude is SpecialValue.ERROR
    assert decode_image_horizon_pixel_pack(error_raw).start_longitude == pytest.approx(143.2)


def test_image_horizon_is_typed_inside_uas_local_set() -> None:
    pack = ImageHorizonPixelPack(0, 36, 56, 0)
    packet = encode_uas_local_set(
        {
            2: datetime(2024, 1, 1, tzinfo=timezone.utc),
            65: 19,
            81: pack,
        }
    )
    decoded = decode_uas_local_set(packet)
    assert decoded.value(81) == pack
    assert encode_field_value(81, pack) == bytes.fromhex("00 24 38 00")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\x00\x24\x38", "between 4 and 20"),
        (b"\x00\x24\x38\x00\x01", "4-byte increments"),
        (b"\x00\x65\x38\x00", "percentage"),
        (b"\x01\x24\x38\x01", "image border"),
        (b"\x00\x24\x00\x24", "must differ"),
    ],
)
def test_image_horizon_rejects_malformed_wire_values(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_image_horizon_pixel_pack(raw)


def test_image_horizon_encoder_rejects_invalid_models() -> None:
    with pytest.raises(TypeError, match="ImageHorizonPixelPack"):
        encode_image_horizon_pixel_pack(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="trailing optional fields"):
        encode_image_horizon_pixel_pack(
            ImageHorizonPixelPack(0, 10, 20, 0, start_longitude=10.0)
        )
    with pytest.raises(ValueError, match="percentage"):
        encode_image_horizon_pixel_pack(ImageHorizonPixelPack(-1, 0, 20, 0))
    with pytest.raises(TypeError, match="must be integers"):
        encode_image_horizon_pixel_pack(
            ImageHorizonPixelPack(True, 0, 20, 0)  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="latitude"):
        encode_image_horizon_pixel_pack(
            ImageHorizonPixelPack(0, 10, 20, 0, start_latitude=91.0)
        )
    with pytest.raises(ValueError, match="longitude"):
        encode_image_horizon_pixel_pack(
            ImageHorizonPixelPack(0, 10, 20, 0, start_latitude=0, start_longitude=-181)
        )
    with pytest.raises(ValueError, match="does not define special"):
        encode_image_horizon_pixel_pack(
            ImageHorizonPixelPack(
                0, 10, 20, 0, start_latitude=SpecialValue.OFF_EARTH
            )
        )


def test_sensor_frame_rate_official_drop_frame_vector() -> None:
    raw = bytes.fromhex("83 D4 60 87 69")
    rate = decode_sensor_frame_rate_pack(raw)
    assert rate == SensorFrameRatePack(60_000, 1_001)
    assert rate.frames_per_second == pytest.approx(59.94005994005994)
    assert rate.ratio.numerator == 60_000
    assert rate.ratio.denominator == 1_001
    assert encode_sensor_frame_rate_pack(rate) == raw


def test_sensor_frame_rate_default_denominator_is_canonically_truncated() -> None:
    assert decode_sensor_frame_rate_pack(b"\x1e") == SensorFrameRatePack(30)
    assert decode_sensor_frame_rate_pack(b"\x1e\x01") == SensorFrameRatePack(30)
    assert encode_sensor_frame_rate_pack(SensorFrameRatePack(30)) == b"\x1e"
    assert encode_sensor_frame_rate_pack(SensorFrameRatePack(30, 1)) == b"\x1e"


def test_sensor_frame_rate_is_typed_inside_uas_local_set() -> None:
    rate = SensorFrameRatePack(30_000, 1_001)
    packet = encode_uas_local_set(
        {
            2: datetime(2024, 1, 1, tzinfo=timezone.utc),
            65: 19,
            127: rate,
        }
    )
    assert decode_uas_local_set(packet).value(127) == rate
    assert encode_field_value(127, rate) == encode_sensor_frame_rate_pack(rate)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "at least one"),
        (b"\x81", "unterminated numerator"),
        (b"\x1e\x81", "unterminated denominator"),
        (b"\x1e\x01\x01", "exactly two"),
        (b"\x1e\x00", "denominator must be positive"),
    ],
)
def test_sensor_frame_rate_rejects_malformed_values(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_sensor_frame_rate_pack(raw)


@pytest.mark.parametrize(
    "rate",
    [
        SensorFrameRatePack(-1),
        SensorFrameRatePack(30, 0),
        SensorFrameRatePack(True),  # type: ignore[arg-type]
    ],
)
def test_sensor_frame_rate_encoder_rejects_invalid_values(rate: SensorFrameRatePack) -> None:
    with pytest.raises((TypeError, ValueError)):
        encode_sensor_frame_rate_pack(rate)


def test_sensor_frame_rate_encoder_rejects_wrong_model_and_oversized_pack() -> None:
    with pytest.raises(TypeError, match="SensorFrameRatePack"):
        encode_sensor_frame_rate_pack(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="16-byte"):
        encode_sensor_frame_rate_pack(SensorFrameRatePack(2**112, 2**112))


def test_control_command_official_vector() -> None:
    raw = bytes.fromhex("05 11") + b"Fly to Waypoint 1"
    command = decode_control_command(raw)
    assert command == ControlCommand(5, "Fly to Waypoint 1")
    assert encode_control_command(command) == raw


def test_control_command_optional_time_round_trip() -> None:
    issued_at = datetime(2024, 2, 3, 4, 5, 6, 789012, tzinfo=timezone.utc)
    command = ControlCommand(128, "Track target", issued_at)
    raw = encode_control_command(command)
    assert raw.startswith(bytes.fromhex("81 00 0C") + b"Track target")
    assert len(raw[-8:]) == 8
    assert decode_control_command(raw) == command
    large_id = ControlCommand(2**80, "Vendor command")
    assert decode_control_command(encode_control_command(large_id)) == large_id


def test_control_command_verification_official_vector() -> None:
    acknowledgements = ControlCommandVerificationList((3, 7))
    assert encode_control_command_verification_list(acknowledgements) == b"\x03\x07"
    assert decode_control_command_verification_list(b"\x03\x07") == acknowledgements


def test_multiple_control_commands_are_valid_in_one_uas_local_set() -> None:
    commands = (ControlCommand(5, "Fly"), ControlCommand(6, "Track"))
    packet = encode_uas_local_set(
        {
            2: datetime(2024, 1, 1, tzinfo=timezone.utc),
            65: 19,
            115: commands,
            116: ControlCommandVerificationList((3, 7)),
        }
    )
    decoded = decode_uas_local_set(packet)
    assert tuple(field.value for field in decoded.getall(115)) == commands
    assert decoded.value(116) == ControlCommandVerificationList((3, 7))
    assert encode_field_value(115, commands[0]) == encode_control_command(commands[0])

    replacements = (ControlCommand(7, "Orbit"), ControlCommand(8, "Return"))
    updated = decode_uas_local_set(update_uas_local_set(packet, {115: replacements}))
    assert tuple(field.value for field in updated.getall(115)) == replacements

    with pytest.raises(ValueError, match="at least one"):
        encode_uas_local_set(
            {
                2: datetime(2024, 1, 1, tzinfo=timezone.utc),
                65: 19,
                115: (),
            }
        )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "Command ID"),
        (b"\x81", "Command ID"),
        (b"\x05\x03ab", "Command String"),
        (b"\x05\x01\xff", "UTF-8"),
        (b"\x05\x01a\x00", "Command Time"),
        (b"\x05\x01a" + bytes(9), "Command Time"),
    ],
)
def test_control_command_rejects_malformed_values(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_control_command(raw)


def test_control_command_encoder_contracts() -> None:
    with pytest.raises(TypeError, match="ControlCommand"):
        encode_control_command(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cannot be negative"):
        encode_control_command(ControlCommand(-1, "Fly"))
    with pytest.raises(ValueError, match="127 characters"):
        encode_control_command(ControlCommand(1, "x" * 128))
    with pytest.raises(ValueError, match="trimmed UTF-8"):
        encode_control_command(ControlCommand(1, " Fly"))
    with pytest.raises(ValueError, match="timezone-aware"):
        encode_control_command(ControlCommand(1, "Fly", datetime(2024, 1, 1)))


def test_control_command_verification_list_contracts() -> None:
    with pytest.raises(DecodeError, match="at least one"):
        decode_control_command_verification_list(b"")
    with pytest.raises(DecodeError, match="unterminated"):
        decode_control_command_verification_list(b"\x03\x81")
    with pytest.raises(TypeError, match="VerificationList"):
        encode_control_command_verification_list(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one"):
        encode_control_command_verification_list(ControlCommandVerificationList(()))
    with pytest.raises(ValueError, match="cannot be negative"):
        encode_control_command_verification_list(ControlCommandVerificationList((3, -1)))


def test_active_wavelength_list_official_vector() -> None:
    wavelengths = ActiveWavelengthList((1, 3))
    assert decode_active_wavelength_list(b"\x01\x03") == wavelengths
    assert encode_active_wavelength_list(wavelengths) == b"\x01\x03"


def test_active_wavelength_zero_is_exclusive() -> None:
    assert decode_active_wavelength_list(b"\x00") == ActiveWavelengthList((0,))
    with pytest.raises(DecodeError, match="ID zero"):
        decode_active_wavelength_list(b"\x00\x01")
    with pytest.raises(ValueError, match="ID zero"):
        encode_active_wavelength_list(ActiveWavelengthList((1, 0)))
    with pytest.raises(DecodeError, match="at least one"):
        decode_active_wavelength_list(b"")


def test_country_codes_official_vector_and_explicit_unknown() -> None:
    raw = bytes.fromhex("01 0E 03 43414E 00 03 465241")
    countries = decode_country_codes(raw)
    assert countries == CountryCodes(
        coding_method=14,
        overflight="CAN",
        operator=SpecialValue.UNKNOWN,
        manufacture="FRA",
    )
    assert encode_country_codes(countries) == raw


def test_country_codes_truncates_optional_trailing_values() -> None:
    countries = CountryCodes(coding_method=13, overflight="US")
    assert encode_country_codes(countries) == bytes.fromhex("01 0D 02 5553")
    assert decode_country_codes(bytes.fromhex("01 0D 02 5553")) == countries
    with pytest.raises(ValueError, match="trailing optional"):
        encode_country_codes(
            CountryCodes(13, "US", operator=None, manufacture="CA")
        )


def test_wavelength_and_country_packs_are_typed_in_uas_local_set() -> None:
    active = ActiveWavelengthList((1, 3))
    countries = CountryCodes(13, "US", SpecialValue.UNKNOWN, "CA")
    packet = encode_uas_local_set(
        {
            2: datetime(2024, 1, 1, tzinfo=timezone.utc),
            65: 19,
            121: active,
            122: countries,
        }
    )
    decoded = decode_uas_local_set(packet)
    assert decoded.value(121) == active
    assert decoded.value(122) == countries


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\x01\x0d", "Overflight"),
        (b"\x00\x01\x00", "Coding Method"),
        (b"\x01\x10\x00", "Coding Method"),
        (b"\x01\x0d\x02U", "truncated"),
        (b"\x01\x0d\x01\xff", "UTF-8"),
        (b"\x01\x0d\x01U\x01S\x01C\x01X", "at most four"),
    ],
)
def test_country_codes_rejects_malformed_values(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_country_codes(raw)


def test_wavelength_and_country_encoder_contracts() -> None:
    with pytest.raises(TypeError, match="ActiveWavelengthList"):
        encode_active_wavelength_list(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one"):
        encode_active_wavelength_list(ActiveWavelengthList(()))
    with pytest.raises(ValueError, match="cannot be negative"):
        encode_active_wavelength_list(ActiveWavelengthList((-1,)))
    with pytest.raises(TypeError, match="CountryCodes"):
        encode_country_codes(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Coding Method"):
        encode_country_codes(CountryCodes(16, "US"))
    with pytest.raises(ValueError, match="Overflight Country"):
        encode_country_codes(CountryCodes(13, None))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ambiguous"):
        encode_country_codes(CountryCodes(13, ""))


def test_wavelengths_list_official_vector() -> None:
    raw = bytes.fromhex("0D 15 000007D0 00000FA0 4E4E4952")
    wavelengths = decode_wavelengths_list(raw)
    assert wavelengths == WavelengthsList(
        (WavelengthRecord(21, 1000.0, 2000.0, "NNIR"),)
    )
    assert encode_wavelengths_list(wavelengths) == raw


def test_wavelengths_list_multiple_records_and_unicode_names() -> None:
    wavelengths = WavelengthsList(
        (
            WavelengthRecord(21, 380, 750, "VIS custom"),
            WavelengthRecord(128, 8_000, 14_000, "Long-wave λ"),
        )
    )
    assert decode_wavelengths_list(encode_wavelengths_list(wavelengths)) == wavelengths


def test_wavelengths_list_is_typed_inside_uas_local_set() -> None:
    wavelengths = WavelengthsList((WavelengthRecord(21, 1000, 2000, "NNIR"),))
    packet = encode_uas_local_set(
        {
            2: datetime(2024, 1, 1, tzinfo=timezone.utc),
            65: 19,
            128: wavelengths,
        }
    )
    decoded = decode_uas_local_set(packet)
    assert decoded.value(128) == WavelengthsList(
        (WavelengthRecord(21, 1000.0, 2000.0, "NNIR"),)
    )
    assert encode_field_value(128, wavelengths) == encode_wavelengths_list(wavelengths)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "at least one"),
        (b"\x00", "empty Wavelength Record"),
        (b"\x01\x81", "Wavelength ID"),
        (b"\x08\x15" + bytes(7), "wavelength bounds"),
        (b"\x09\x15" + bytes(8), "Name"),
        (b"\x0a\x15" + bytes(8) + b"\xff", "UTF-8"),
        (b"\x0a\x07" + bytes(8) + b"X", "custom ID"),
    ],
)
def test_wavelengths_list_rejects_malformed_values(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_wavelengths_list(raw)


def test_wavelengths_list_encoder_contracts() -> None:
    with pytest.raises(TypeError, match="WavelengthsList"):
        encode_wavelengths_list(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one"):
        encode_wavelengths_list(WavelengthsList(()))
    with pytest.raises(ValueError, match="custom ID"):
        encode_wavelengths_list(WavelengthsList((WavelengthRecord(20, 1, 2, "bad"),)))
    with pytest.raises(ValueError, match=r"minimum.*maximum"):
        encode_wavelengths_list(WavelengthsList((WavelengthRecord(21, 2, 1, "bad"),)))
    with pytest.raises(ValueError, match="outside"):
        encode_wavelengths_list(
            WavelengthsList((WavelengthRecord(21, 0, 1_000_000_001, "bad"),))
        )
    with pytest.raises(ValueError, match="Name is mandatory"):
        encode_wavelengths_list(WavelengthsList((WavelengthRecord(21, 1, 2, ""),)))
    with pytest.raises(ValueError, match="duplicate Wavelength ID"):
        encode_wavelengths_list(
            WavelengthsList(
                (
                    WavelengthRecord(21, 1, 2, "one"),
                    WavelengthRecord(21, 2, 3, "two"),
                )
            )
        )


def test_airbase_locations_official_vector() -> None:
    raw = bytes.fromhex(
        "0B 406BC209 19BDA554 070E00 0B 40783CB8 19A29274 07C600"
    )
    locations = decode_airbase_locations(raw)
    assert isinstance(locations.takeoff, AirbaseLocation)
    assert isinstance(locations.recovery, AirbaseLocation)
    assert locations.takeoff.latitude == pytest.approx(38.841859, abs=1e-6)
    assert locations.takeoff.longitude == pytest.approx(-77.036784, abs=1e-6)
    assert locations.takeoff.hae == pytest.approx(3, abs=0.01)
    assert locations.recovery.latitude == pytest.approx(38.939353, abs=1e-6)
    assert locations.recovery.longitude == pytest.approx(-77.459811, abs=1e-6)
    assert locations.recovery.hae == pytest.approx(95, abs=0.01)
    assert encode_airbase_locations(locations) == raw


def test_airbase_location_truncation_defaults_recovery_to_takeoff() -> None:
    takeoff = AirbaseLocation(38.8, -77.0)
    locations = AirbaseLocations(takeoff)
    raw = encode_airbase_locations(locations)
    assert len(raw) == 9
    decoded = decode_airbase_locations(raw)
    assert decoded.recovery is None
    assert decoded.effective_recovery == decoded.takeoff
    assert isinstance(decoded.takeoff, AirbaseLocation)
    assert decoded.takeoff.hae is None


def test_airbase_location_explicit_unknown_is_preserved() -> None:
    recovery = AirbaseLocation(40, -75, 100)
    locations = AirbaseLocations(SpecialValue.UNKNOWN, recovery)
    raw = encode_airbase_locations(locations)
    assert raw[0] == 0
    decoded = decode_airbase_locations(raw)
    assert decoded.takeoff is SpecialValue.UNKNOWN
    assert decoded.recovery == decoded.effective_recovery


def test_airbase_locations_are_typed_inside_uas_local_set() -> None:
    locations = AirbaseLocations(AirbaseLocation(38.8, -77.0, 3))
    packet = encode_uas_local_set(
        {
            2: datetime(2024, 1, 1, tzinfo=timezone.utc),
            65: 19,
            130: locations,
        }
    )
    decoded = decode_uas_local_set(packet)
    assert isinstance(decoded.value(130), AirbaseLocations)
    assert encode_field_value(130, locations) == encode_airbase_locations(locations)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "at least a take-off"),
        (b"\x01\x00", "8 or 11 bytes"),
        (b"\x08" + bytes.fromhex("D0000000") + bytes(4), "IMAP specials"),
        (b"\x08" + bytes(8) + b"\x00\x00", "at most two"),
        (b"\x00\x00", "both.*Unknown"),
    ],
)
def test_airbase_locations_rejects_malformed_values(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_airbase_locations(raw)


def test_airbase_locations_encoder_contracts() -> None:
    with pytest.raises(TypeError, match="AirbaseLocations"):
        encode_airbase_locations(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"both.*Unknown"):
        encode_airbase_locations(AirbaseLocations(SpecialValue.UNKNOWN))
    with pytest.raises(ValueError, match="latitude"):
        encode_airbase_locations(AirbaseLocations(AirbaseLocation(91, 0)))
    with pytest.raises(ValueError, match="longitude"):
        encode_airbase_locations(AirbaseLocations(AirbaseLocation(0, -181)))
    with pytest.raises(ValueError, match="HAE"):
        encode_airbase_locations(AirbaseLocations(AirbaseLocation(0, 0, 9001)))
    with pytest.raises(ValueError, match="does not allow"):
        encode_airbase_locations(AirbaseLocations(SpecialValue.OFF_EARTH))

    same = AirbaseLocation(38.8, -77.0, 3)
    assert encode_airbase_locations(AirbaseLocations(same, same)) == encode_airbase_locations(
        AirbaseLocations(same)
    )


def test_payload_list_official_vector() -> None:
    raw = (
        bytes.fromhex("03 12 00 00 0F")
        + b"VIS Nose Camera"
        + bytes.fromhex("15 01 00 12")
        + b"ACME VIS Model 123"
        + bytes.fromhex("14 02 00 11")
        + b"ACME IR Model 456"
    )
    payloads = PayloadList(
        3,
        (
            PayloadRecord(0, 0, "VIS Nose Camera"),
            PayloadRecord(1, 0, "ACME VIS Model 123"),
            PayloadRecord(2, 0, "ACME IR Model 456"),
        ),
    )
    assert decode_payload_list(raw) == payloads
    assert encode_payload_list(payloads) == raw


def test_distributed_payload_list_allows_a_valid_subset() -> None:
    partial = PayloadList(
        6,
        (PayloadRecord(2, 2, "Radar"), PayloadRecord(5, 3, "SIGINT")),
    )
    assert decode_payload_list(encode_payload_list(partial)) == partial


def test_active_payloads_bitset_official_vector_and_multiple_bytes() -> None:
    assert decode_active_payloads(b"\x0b") == ActivePayloads(frozenset({0, 1, 3}))
    assert encode_active_payloads(ActivePayloads(frozenset({0, 1, 3}))) == b"\x0b"
    active = ActivePayloads(frozenset({0, 8, 15}))
    assert encode_active_payloads(active) == bytes.fromhex("01 81")
    assert decode_active_payloads(bytes.fromhex("01 81")) == active
    assert encode_active_payloads(ActivePayloads(frozenset())) == b"\x00"


def test_payload_items_are_typed_inside_uas_local_set() -> None:
    payloads = PayloadList(1, (PayloadRecord(0, 0, "Camera"),))
    active = ActivePayloads(frozenset({0}))
    packet = encode_uas_local_set(
        {
            2: datetime(2024, 1, 1, tzinfo=timezone.utc),
            65: 19,
            138: payloads,
            139: active,
        }
    )
    decoded = decode_uas_local_set(packet)
    assert decoded.value(138) == payloads
    assert decoded.value(139) == active


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "Payload Count"),
        (b"\x01", "at least one record"),
        (b"\x01\x00", "empty Payload Record"),
        (b"\x01\x01\x81", "Payload ID"),
        (b"\x01\x04\x00\x05\x01X", "Payload Type"),
        (b"\x01\x04\x00\x00\x02X", "Payload Name"),
        (b"\x01\x04\x00\x00\x01\xff", "UTF-8"),
        (b"\x01\x04\x01\x00\x01X", "outside Payload Count"),
    ],
)
def test_payload_list_rejects_malformed_values(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_payload_list(raw)


def test_payload_encoder_contracts() -> None:
    with pytest.raises(TypeError, match="PayloadList"):
        encode_payload_list(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Payload Count"):
        encode_payload_list(PayloadList(-1, ()))
    with pytest.raises(ValueError, match="at least one record"):
        encode_payload_list(PayloadList(1, ()))
    with pytest.raises(ValueError, match="Payload Type"):
        encode_payload_list(PayloadList(1, (PayloadRecord(0, 5, "bad"),)))
    with pytest.raises(ValueError, match="Payload Name"):
        encode_payload_list(PayloadList(1, (PayloadRecord(0, 0, ""),)))
    with pytest.raises(ValueError, match="sequential"):
        encode_payload_list(
            PayloadList(
                2,
                (PayloadRecord(0, 0, "one"), PayloadRecord(0, 0, "duplicate")),
            )
        )


def test_active_payload_encoder_contracts() -> None:
    with pytest.raises(DecodeError, match="at least one byte"):
        decode_active_payloads(b"")
    with pytest.raises(TypeError, match="ActivePayloads"):
        encode_active_payloads(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cannot be negative"):
        encode_active_payloads(ActivePayloads(frozenset({-1})))


def test_weapons_stores_official_vector() -> None:
    raw = (
        bytes.fromhex("0E 01 01 01 03 8203 07")
        + b"Harpoon"
        + bytes.fromhex("0F 01 01 02 02 9E04 08")
        + b"Hellfire"
        + bytes.fromhex("0C 01 02 01 01 03 06")
        + b"GBU-15"
    )
    stores = decode_weapons_stores(raw)
    assert len(stores.records) == 3
    first = stores.records[0]
    assert (first.station_id, first.hardpoint_id, first.carriage_id, first.store_id) == (
        1,
        1,
        1,
        3,
    )
    assert first.status == WeaponStatus(3, fuze_enabled=True)
    assert stores.records[1].status == WeaponStatus(
        4,
        fuze_enabled=True,
        laser_enabled=True,
        target_enabled=True,
        weapon_armed=True,
    )
    assert encode_weapons_stores(stores) == raw


def test_weapon_status_raw_layout() -> None:
    status = WeaponStatus(
        11, fuze_enabled=True, target_enabled=True, weapon_armed=True
    )
    assert status.raw == 0xD0B
    assert WeaponStatus.from_raw(status.raw) == status


def test_weapons_stores_are_typed_inside_uas_local_set() -> None:
    stores = WeaponsStores(
        (WeaponStore(1, 2, 3, 4, WeaponStatus(3), "Test weapon"),)
    )
    packet = encode_uas_local_set(
        {
            2: datetime(2024, 1, 1, tzinfo=timezone.utc),
            65: 19,
            140: stores,
        }
    )
    assert decode_uas_local_set(packet).value(140) == stores
    assert encode_field_value(140, stores) == encode_weapons_stores(stores)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "at least one"),
        (b"\x00", "empty Weapons Record"),
        (b"\x01\x81", "Station ID"),
        (b"\x05\x01\x01\x01\x01\x80", "Status"),
        (b"\x08\x01\x01\x01\x01\x81\x00\x01X", "General Status"),
        (b"\x08\x01\x01\x01\x01\xA0\x00\x01X", "reserved status"),
        (b"\x07\x01\x01\x01\x01\x03\x02X", "Weapon Type"),
    ],
)
def test_weapons_stores_rejects_malformed_values(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_weapons_stores(raw)


def test_weapons_stores_encoder_contracts() -> None:
    with pytest.raises(TypeError, match="WeaponsStores"):
        encode_weapons_stores(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one"):
        encode_weapons_stores(WeaponsStores(()))
    with pytest.raises(ValueError, match="General Status"):
        encode_weapons_stores(
            WeaponsStores((WeaponStore(1, 1, 1, 1, WeaponStatus(128), "bad"),))
        )
    with pytest.raises(ValueError, match="Weapon Type"):
        encode_weapons_stores(
            WeaponsStores((WeaponStore(1, 1, 1, 1, WeaponStatus(1), ""),))
        )
    duplicate = WeaponStore(1, 1, 1, 1, WeaponStatus(1), "one")
    with pytest.raises(ValueError, match="duplicate weapon address"):
        encode_weapons_stores(WeaponsStores((duplicate, duplicate)))


def test_waypoint_list_official_vector() -> None:
    raw = bytes.fromhex(
        "0F 00 0001 03 4071D894 19BDBFE7 089800 "
        "0F 01 0002 02 4071D388 19BCCE24 08FC00 "
        "0F 02 7FFF 01 4071E308 19BF2C1B 07D000 "
        "0F 03 FFFE 00 4071E5AF 19BF5AA7 096000"
    )
    waypoints = decode_waypoint_list(raw)
    assert tuple(record.waypoint_id for record in waypoints.records) == (0, 1, 2, 3)
    assert tuple(record.prosecution_order for record in waypoints.records) == (
        1,
        2,
        32767,
        -2,
    )
    assert waypoints.records[0].info == WaypointInfo(
        manual=True, ad_hoc=True
    )
    assert waypoints.records[1].info == WaypointInfo(manual=False, ad_hoc=True)
    assert waypoints.records[2].info == WaypointInfo(manual=True, ad_hoc=False)
    assert waypoints.records[3].info == WaypointInfo(manual=False, ad_hoc=False)
    location = waypoints.records[0].location
    assert isinstance(location, AirbaseLocation)
    assert location.latitude == pytest.approx(38.889422, abs=0.000001)
    assert location.longitude == pytest.approx(-77.035162, abs=0.000001)
    assert location.hae == pytest.approx(200, abs=0.001)
    assert encode_waypoint_list(waypoints) == raw


def test_waypoint_list_supports_trailing_optional_fields() -> None:
    waypoints = WaypointList(
        (
            WaypointRecord(4, 0),
            WaypointRecord(5, 1, WaypointInfo(manual=True)),
            WaypointRecord(
                6,
                2,
                WaypointInfo(ad_hoc=True),
                AirbaseLocation(10, 20),
            ),
        )
    )
    encoded = encode_waypoint_list(waypoints)
    decoded = decode_waypoint_list(encoded)
    assert decoded.records[:2] == waypoints.records[:2]
    assert decoded.records[2].location == waypoints.records[2].location


def test_waypoint_list_is_typed_inside_uas_local_set() -> None:
    waypoints = WaypointList((WaypointRecord(1, 0),))
    packet = encode_uas_local_set(
        {
            2: datetime(2024, 1, 1, tzinfo=timezone.utc),
            65: 19,
            141: waypoints,
        }
    )
    assert decode_uas_local_set(packet).value(141) == waypoints
    assert encode_field_value(141, waypoints) == encode_waypoint_list(waypoints)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "at least one"),
        (b"\x00", "empty Waypoint Record"),
        (b"\x01\x81", "Waypoint ID"),
        (b"\x02\x01\x00", "Prosecution Order"),
        (b"\x04\x01\x00\x00\x04", "reserved Info Value"),
        (b"\x05\x01\x00\x00\x00\x01", "Location"),
        (b"\x03\x01\x00\x00\x03\x01\x00\x01", "duplicate Waypoint ID"),
        (b"\x03\x01\x00\x01\x03\x02\x00\x01", "Prosecution Orders"),
    ],
)
def test_waypoint_list_rejects_malformed_values(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_waypoint_list(raw)


def test_waypoint_list_allows_repeated_cancelled_order() -> None:
    value = WaypointList((WaypointRecord(1, 32767), WaypointRecord(2, 32767)))
    assert decode_waypoint_list(encode_waypoint_list(value)) == value


def test_waypoint_list_encoder_contracts() -> None:
    with pytest.raises(TypeError, match="WaypointList"):
        encode_waypoint_list(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one"):
        encode_waypoint_list(WaypointList(()))
    with pytest.raises(ValueError, match="Waypoint ID"):
        encode_waypoint_list(WaypointList((WaypointRecord(-1, 0),)))
    with pytest.raises(ValueError, match="Prosecution Order"):
        encode_waypoint_list(WaypointList((WaypointRecord(1, 32768),)))
    with pytest.raises(ValueError, match="duplicate Waypoint ID"):
        encode_waypoint_list(
            WaypointList((WaypointRecord(1, 0), WaypointRecord(1, 1)))
        )
    with pytest.raises(ValueError, match="Prosecution Orders"):
        encode_waypoint_list(
            WaypointList((WaypointRecord(1, 0), WaypointRecord(2, 0)))
        )
    with pytest.raises(ValueError, match="Info Value"):
        encode_waypoint_list(
            WaypointList(
                (
                    WaypointRecord(
                        1, 0, None, AirbaseLocation(10, 20)
                    ),
                )
            )
        )


def test_view_domain_official_truncated_vector() -> None:
    raw = bytes.fromhex("06 348000 4B0000 06 1A4000 0C8000")
    domain = decode_view_domain(raw)
    assert isinstance(domain.azimuth, ViewDomainPair)
    assert isinstance(domain.elevation, ViewDomainPair)
    assert domain.azimuth.start == pytest.approx(210.0)
    assert domain.azimuth.angular_range == pytest.approx(300.0)
    assert domain.elevation.start == pytest.approx(-75.0)
    assert domain.elevation.angular_range == pytest.approx(50.0)
    assert domain.roll is None
    assert encode_view_domain(domain) == raw


def test_view_domain_preserves_unknown_and_variable_precision_pairs() -> None:
    domain = ViewDomain(
        SpecialValue.UNKNOWN,
        ViewDomainPair(-75, 50),
        ViewDomainPair(350, 20),
    )
    raw = encode_view_domain(domain, value_length=2)
    assert raw[0] == 0
    decoded = decode_view_domain(raw)
    assert decoded.azimuth is SpecialValue.UNKNOWN
    assert isinstance(decoded.elevation, ViewDomainPair)
    assert isinstance(decoded.roll, ViewDomainPair)
    assert decoded.elevation.start == pytest.approx(-75, abs=0.01)
    assert decoded.roll.end == pytest.approx(370, abs=0.01)
    assert decoded.roll.normalized_end == pytest.approx(10, abs=0.01)
    assert encode_view_domain(decoded, value_length=2) == raw


def test_view_domain_is_typed_inside_uas_local_set() -> None:
    domain = ViewDomain(ViewDomainPair(210, 300))
    packet = encode_uas_local_set(
        {
            2: datetime(2024, 1, 1, tzinfo=timezone.utc),
            65: 19,
            142: domain,
        }
    )
    decoded = decode_uas_local_set(packet).value(142)
    assert isinstance(decoded, ViewDomain)
    assert encode_field_value(142, domain) == encode_view_domain(domain)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "at least one"),
        (b"\x01\x00", "positive even"),
        (b"\x02\xc0\x00", "special"),
        (b"\x02\x00\x00\x02\x00\x00\x02\x00\x00\x00", "at most three"),
    ],
)
def test_view_domain_rejects_malformed_values(raw: bytes, message: str) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_view_domain(raw)


def test_view_domain_encoder_contracts() -> None:
    with pytest.raises(TypeError, match="ViewDomain"):
        encode_view_domain(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one"):
        encode_view_domain(ViewDomain())
    with pytest.raises(ValueError, match="trailing"):
        encode_view_domain(ViewDomain(None, ViewDomainPair(-75, 50)))
    with pytest.raises(ValueError, match="azimuth start"):
        encode_view_domain(ViewDomain(ViewDomainPair(-1, 20)))
    with pytest.raises(ValueError, match="angular range"):
        encode_view_domain(ViewDomain(ViewDomainPair(10, 361)))
    with pytest.raises(ValueError, match="value_length"):
        encode_view_domain(ViewDomain(ViewDomainPair(10, 20)), value_length=0)


def test_metadata_substream_id_official_uuid_vector() -> None:
    raw = bytes.fromhex("00 8DC4F4623EA25A859C5D0AF0C95E8C39")
    identifier = decode_metadata_substream_id(raw)
    assert identifier == MetadataSubstreamID(
        0, UUID("8dc4f462-3ea2-5a85-9c5d-0af0c95e8c39")
    )
    assert encode_metadata_substream_id(identifier) == raw


def test_metadata_substream_id_local_form_truncates_uuid() -> None:
    identifier = MetadataSubstreamID(300)
    raw = encode_metadata_substream_id(identifier)
    assert raw == bytes.fromhex("822C")
    assert decode_metadata_substream_id(raw) == identifier


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "requires a Local ID"),
        (b"\x00", "requires a UUID"),
        (b"\x01" + bytes(16), "omit the UUID"),
        (b"\x00" + bytes(15), "requires a UUID"),
        (b"\x81", "invalid Local ID"),
        (bytes(18), "17-byte maximum"),
    ],
)
def test_metadata_substream_id_rejects_malformed_values(
    raw: bytes, message: str
) -> None:
    with pytest.raises(DecodeError, match=message):
        decode_metadata_substream_id(raw)


def test_metadata_substream_id_encoder_contracts() -> None:
    uid = UUID("8dc4f462-3ea2-5a85-9c5d-0af0c95e8c39")
    with pytest.raises(TypeError, match="MetadataSubstreamID"):
        encode_metadata_substream_id(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cannot be negative"):
        encode_metadata_substream_id(MetadataSubstreamID(-1))
    with pytest.raises(ValueError, match="requires a UUID"):
        encode_metadata_substream_id(MetadataSubstreamID(0))
    with pytest.raises(ValueError, match="omit the UUID"):
        encode_metadata_substream_id(MetadataSubstreamID(1, uid))
