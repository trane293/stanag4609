"""Optional reference-player support built on the transport and KLV core."""

from stanag4609.player.live import (
    BoundedBroadcast,
    BroadcastPoll,
    FragmentedMP4Buffer,
    LiveMetadataDecoder,
    LivePlayerGateway,
    LivePlayerStats,
    MP4Initialization,
    ffmpeg_live_player_command,
)
from stanag4609.player.timeline import (
    MetadataSample,
    MetadataTimeline,
    OverlayDetection,
    OverlayMaskRun,
    extract_overlay_detections,
    scan_transport_file,
    scan_transport_timeline,
)

__all__ = [
    "BoundedBroadcast",
    "BroadcastPoll",
    "FragmentedMP4Buffer",
    "LiveMetadataDecoder",
    "LivePlayerGateway",
    "LivePlayerStats",
    "MP4Initialization",
    "MetadataSample",
    "MetadataTimeline",
    "OverlayDetection",
    "OverlayMaskRun",
    "extract_overlay_detections",
    "ffmpeg_live_player_command",
    "scan_transport_file",
    "scan_transport_timeline",
]
