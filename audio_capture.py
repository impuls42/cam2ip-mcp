#!/usr/bin/env python3
"""Record a short clip from the camera's microphone array.

A USB webcam registers as two independent devices: a V4L2 video node and an ALSA
capture device. They share a cable and nothing else -- opening one has no bearing
on the other -- so this neither needs nor disturbs the video stream.

Capture goes through arecord rather than a Python ALSA binding. The binding would
be a C extension to build for two architectures on a musl base, for the sake of
one blocking read of a fixed number of frames; arecord is a 40-line subprocess
call that alsa-utils already provides.

Level analysis rides along with the clip because it is the part a text-only
client can act on. "Is the extractor still running", "did something fall over in
the workshop" and "is this microphone even connected" are all answered by a
number, and none of them need the audio to be listened to.
"""

from __future__ import annotations

import array
import asyncio
import io
import math
import os
import re
import shutil
import subprocess
import wave

# The mic array offers 48000, 32000 and 16000. 16k is the default because
# nothing this is for -- is there noise, is a machine running, is someone
# speaking -- is improved by three times the data.
DEFAULT_RATE = 16000
DEFAULT_CHANNELS = 1

# A ceiling on one call, not a policy about how much may be recorded overall. It
# exists so a mistyped duration cannot hold the microphone open for an hour or
# return a payload nothing can carry: 60s of 16kHz mono is about 2MB of WAV.
MAX_SECONDS = 60.0

# `arecord -l` rather than /proc/asound/cards, which is the obvious source and
# the wrong one: Docker gives a container its own /proc, and the ALSA entries are
# not in it. Inside the container that file is missing entirely while recording
# works perfectly, because ALSA enumerates through /dev/snd/controlC* instead --
# which is exactly what arecord does. Listing capture devices only, and giving
# the device index alongside the card number, are the incidental wins.
CARD_LINE = re.compile(
    r"^card (?P<card>\d+): (?P<short>\S+) \[(?P<name>[^\]]*)\], "
    r"device (?P<device>\d+): (?P<detail>.*)$",
    re.MULTILINE,
)


class AudioCaptureError(RuntimeError):
    """Recording failed, or there is nothing to record from."""


def list_capture_devices() -> list[tuple[str, str, str]]:
    """(card number, device number, description) for each ALSA capture device."""
    arecord = shutil.which("arecord")
    if arecord is None:
        raise AudioCaptureError(
            "arecord is not installed, so audio devices cannot be listed "
            "(alsa-utils provides it)"
        )
    try:
        completed = subprocess.run(
            [arecord, "-l"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioCaptureError(f"could not list audio devices: {exc}") from None

    return [
        (m["card"], m["device"], f"{m['short']} {m['name']} {m['detail']}")
        for m in CARD_LINE.finditer(completed.stdout)
    ]


def find_device(hint: str | None = None) -> str:
    """Resolve an ALSA capture device, preferring the camera's own microphone.

    A hint that already looks like an ALSA device name is passed through
    untouched, so an operator can name a card this never would -- a separate
    interface, a loopback, "default". Otherwise it is matched against the card
    descriptions, which is how "S600" finds the right card without anyone having
    to know its number: USB card numbering follows probe order and moves across
    reboots.
    """
    if hint and (hint.startswith(("hw:", "plughw:", "sysdefault", "default")) or "," in hint):
        return hint

    cards = list_capture_devices()
    if not cards:
        raise AudioCaptureError(
            "no capture devices found. In a container this usually means /dev/snd "
            "was not passed in (--device=/dev/snd)"
        )

    wanted = (hint or "").strip().lower()
    for card, device, description in cards:
        if wanted and wanted in description.lower():
            return f"plughw:{card},{device}"

    if wanted:
        names = ", ".join(d.strip() for _, _, d in cards)
        raise AudioCaptureError(f"no capture device matching {hint!r}. Found: {names}")

    # With nothing asked for, prefer a USB card. This server is attached to a
    # webcam, whose microphone is a USB audio device, while card 0 on a laptop is
    # the built-in one pointed at whoever is sitting there -- so falling through
    # to "the first card" would quietly record the wrong microphone, which is the
    # one kind of wrong default worth going out of the way to avoid.
    for card, device, description in cards:
        if "usb" in description.lower():
            return f"plughw:{card},{device}"

    # plughw rather than hw: it lets ALSA convert rate and channel count, so a
    # request for 16kHz mono works on a device that only captures 48kHz stereo
    # instead of failing with "Sample format non available".
    return f"plughw:{cards[0][0]},{cards[0][1]}"


async def record(
    device: str,
    seconds: float,
    rate: int = DEFAULT_RATE,
    channels: int = DEFAULT_CHANNELS,
) -> bytes:
    """Capture `seconds` of audio and return it as a WAV file."""
    if seconds <= 0:
        raise ValueError("seconds must be greater than 0")
    if seconds > MAX_SECONDS:
        raise ValueError(f"seconds must be at most {MAX_SECONDS:g}")

    arecord = shutil.which("arecord")
    if arecord is None:
        raise AudioCaptureError(
            "arecord is not installed, so audio cannot be captured "
            "(alsa-utils provides it)"
        )

    process = await asyncio.create_subprocess_exec(
        arecord,
        "-D", device,
        "-f", "S16_LE",
        "-r", str(rate),
        "-c", str(channels),
        "-d", str(math.ceil(seconds)),
        "-t", "wav",
        "-q",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        # arecord stops itself after -d seconds; the timeout is for the case
        # where it wedges on a device that never delivers, which a USB
        # microphone being unplugged mid-capture will do.
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=seconds + 15
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise AudioCaptureError(
            f"arecord did not finish within {seconds + 15:.0f}s on {device}"
        ) from None

    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip() or f"exit {process.returncode}"
        raise AudioCaptureError(f"arecord failed on {device}: {detail}")
    if not stdout:
        raise AudioCaptureError(f"arecord returned no audio from {device}")

    return stdout


def _dbfs(value: float, full_scale: float = 32768.0) -> float:
    """Level in dBFS. 0 is full scale; silence floors at -120 rather than -inf."""
    if value <= 0:
        return -120.0
    return max(-120.0, round(20 * math.log10(value / full_scale), 1))


def analyse(wav_bytes: bytes) -> dict[str, object]:
    """Levels and a plain-language verdict for a recorded clip.

    The verdict exists because dBFS is not self-explanatory, and the most common
    question is the least quantitative one: is this microphone working, and is
    there anything on it. A caller that wants the numbers still has them.
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.getnframes()
        raw = handle.readframes(frames)

    if width != 2:
        raise AudioCaptureError(f"expected 16-bit samples, got {width * 8}-bit")

    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % 2])
    if not samples:
        raise AudioCaptureError("the recording contained no samples")

    peak = max(abs(int(s)) for s in samples)
    # Sum of squares in Python is slow on long clips but honest; a minute of
    # 16kHz mono is under a million samples, which is a few hundred milliseconds
    # and happens off the event loop anyway.
    rms = math.sqrt(sum(float(s) * s for s in samples) / len(samples))
    clipped = sum(1 for s in samples if abs(int(s)) >= 32700)

    rms_dbfs = _dbfs(rms)
    if peak == 0:
        verdict = "silent -- not a quiet room but a dead channel, every sample is zero"
    elif rms_dbfs < -60:
        verdict = "near silence"
    elif rms_dbfs < -40:
        verdict = "quiet: background noise, no nearby source"
    elif rms_dbfs < -20:
        verdict = "moderate: conversation or machinery at a distance"
    else:
        verdict = "loud: a source close to the microphone"
    if clipped > len(samples) // 1000:
        verdict += "; clipping, so the true level is higher than measured"

    return {
        "seconds": round(frames / rate, 2) if rate else None,
        "sample_rate": rate,
        "channels": channels,
        "peak_dbfs": _dbfs(peak),
        "rms_dbfs": rms_dbfs,
        "clipped_samples": clipped,
        "verdict": verdict,
    }


def available(device_hint: str | None = None) -> bool:
    """Whether recording could work here, for deciding to offer the tool."""
    if shutil.which("arecord") is None or not os.path.exists("/dev/snd"):
        return False
    try:
        find_device(device_hint)
    except AudioCaptureError:
        return False
    return True
