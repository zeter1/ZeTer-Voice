"""Supported MP3 output values for settings and recovery manifests."""
from typing import NamedTuple

BITRATES = ("64k", "96k", "128k", "160k", "192k")
CHANNELS = {0: "Авто", 1: "1 (моно)", 2: "2 (стерео)"}
SAMPLE_RATES = {32000: "32 000 Гц", 44100: "44 100 Гц", 48000: "48 000 Гц"}


class Mp3Format(NamedTuple):
    bitrate: str
    channels: int
    sample_rate_hz: int


def read_mp3_format(settings: dict, *, sample_rate_default: int = 44100) -> Mp3Format:
    bitrate = settings.get("bitrate")
    channels = settings.get("mp3_channels")
    sample_rate = settings.get("mp3_sample_rate_hz")
    return Mp3Format(
        bitrate if bitrate in BITRATES else "96k",
        channels if type(channels) is int and channels in CHANNELS else 0,
        sample_rate if type(sample_rate) is int and sample_rate in SAMPLE_RATES else sample_rate_default,
    )
