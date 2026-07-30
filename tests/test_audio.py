"""Microphone capture: device selection and what the level analysis claims.

Device selection is the part with a wrong answer that looks right -- recording
the machine's built-in microphone instead of the webcam's produces a perfectly
good clip of the wrong room -- so most of these are about which card gets picked.
"""

from __future__ import annotations

import io
import math
import struct
import wave

import pytest

import audio_capture
from audio_capture import AudioCaptureError, analyse, find_device

# Real `arecord -l` output. The card numbers are not the interesting part; the
# descriptions are, since that is what a hint matches against.
TWO_CARDS = """\
**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: CX20632 Analog [CX20632 Analog]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 1: S600 [EMEET SmartCam S600], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""

BUILT_IN_ONLY = """\
**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: CX20632 Analog [CX20632 Analog]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""

NONE_AT_ALL = ""


class Completed:
    """The parts of subprocess.CompletedProcess that list_capture_devices reads."""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.returncode = 0


@pytest.fixture
def cards(monkeypatch):
    """Stand in for `arecord -l`, which is how capture devices are enumerated.

    Deliberately not /proc/asound/cards: that file is absent inside a container
    even when recording works fine, because Docker gives the container its own
    /proc. Testing against the source actually used is the whole point -- the
    first version of this used the file, and passed while the container failed.
    """
    monkeypatch.setattr(audio_capture.shutil, "which", lambda _: "/usr/bin/arecord")

    def use(listing: str) -> None:
        monkeypatch.setattr(
            audio_capture.subprocess, "run", lambda *a, **k: Completed(listing)
        )

    use(TWO_CARDS)
    return use


def wav_bytes(samples, rate=16000, channels=1) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return out.getvalue()


def tone(amplitude: int, count: int = 16000) -> list[int]:
    return [int(amplitude * math.sin(i * 0.1)) for i in range(count)]


class TestDeviceSelection:
    def test_prefers_the_usb_card_over_the_built_in_one(self, cards):
        """The webcam's microphone is the USB one. Card 0 is the laptop's own,
        pointed at whoever is sitting in front of it."""
        assert find_device(None) == "plughw:1,0"

    def test_matches_a_card_by_name(self, cards):
        assert find_device("S600") == "plughw:1,0"
        assert find_device("HDA Intel") == "plughw:0,0"

    def test_falls_back_to_the_first_card_when_none_is_usb(self, cards):
        cards(BUILT_IN_ONLY)

        assert find_device(None) == "plughw:0,0"

    def test_passes_an_explicit_alsa_name_straight_through(self, cards):
        """An operator naming hw:2,0 or "default" means it, and the device list
        has no say -- a loopback or a second interface will not be in there."""
        assert find_device("hw:2,0") == "hw:2,0"
        assert find_device("default") == "default"

    def test_an_unmatched_hint_lists_what_is_there(self, cards):
        with pytest.raises(AudioCaptureError, match="S600"):
            find_device("Blue Yeti")

    def test_no_capture_devices_points_at_the_container(self, cards):
        cards(NONE_AT_ALL)

        with pytest.raises(AudioCaptureError, match=r"--device=/dev/snd"):
            find_device(None)


class TestAnalysis:
    def test_reports_the_format_it_was_given(self):
        result = analyse(wav_bytes(tone(1000, 8000), rate=8000))

        assert result["sample_rate"] == 8000
        assert result["channels"] == 1
        assert result["seconds"] == 1.0

    def test_distinguishes_a_dead_channel_from_a_quiet_room(self):
        """Both are near-inaudible on a short clip, and only one is a fault."""
        dead = analyse(wav_bytes([0] * 16000))
        quiet = analyse(wav_bytes(tone(30)))

        assert "dead channel" in dead["verdict"]
        assert dead["peak_dbfs"] == -120.0
        assert "dead channel" not in quiet["verdict"]

    def test_level_rises_with_amplitude(self):
        quiet = analyse(wav_bytes(tone(100)))
        loud = analyse(wav_bytes(tone(20000)))

        assert quiet["rms_dbfs"] < loud["rms_dbfs"]
        assert loud["peak_dbfs"] > -10

    def test_full_scale_is_zero_dbfs(self):
        result = analyse(wav_bytes([32767, -32768] * 100))

        assert result["peak_dbfs"] == pytest.approx(0.0, abs=0.1)

    def test_flags_clipping_because_the_measured_level_understates_it(self):
        result = analyse(wav_bytes([32767] * 8000 + [0] * 8000))

        assert result["clipped_samples"] > 0
        assert "clipping" in result["verdict"]

    def test_rejects_a_recording_with_no_samples(self):
        with pytest.raises(AudioCaptureError, match="no samples"):
            analyse(wav_bytes([]))


class TestRecordingGuards:
    async def test_refuses_a_duration_beyond_the_cap(self):
        with pytest.raises(ValueError, match="at most 60"):
            await audio_capture.record("plughw:1,0", 3600)

    @pytest.mark.parametrize("seconds", [0, -1])
    async def test_refuses_a_non_positive_duration(self, seconds):
        with pytest.raises(ValueError, match="greater than 0"):
            await audio_capture.record("plughw:1,0", seconds)

    async def test_says_so_when_arecord_is_missing(self, monkeypatch):
        monkeypatch.setattr(audio_capture.shutil, "which", lambda _: None)

        with pytest.raises(AudioCaptureError, match="alsa-utils"):
            await audio_capture.record("plughw:1,0", 1.0)
