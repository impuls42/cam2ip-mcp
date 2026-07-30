# EMEET SmartCam S600

What this camera can actually be told to do, measured on the unit this server
runs against (USB `328f:00ad`, firmware as shipped, Linux `uvcvideo`). Written
down because most of it is not in the datasheet, and the parts that are turned
out not to be true in the way the marketing implies.

## Standard UVC controls

All of these are real, writable, and take effect on a stream that is already
running — which is what lets this server drive them beside cam2ip rather than
having to own the camera.

| Control | Range | Notes |
|---|---|---|
| `zoom_absolute` | 0–100 | Digital. Crops ahead of the scaling to output resolution. |
| `focus_absolute` | 0–1023 | Higher is nearer. Inactive while autofocus is on. |
| `focus_automatic_continuous` | 0/1 | |
| `auto_exposure` | 1 = manual, 3 = aperture priority | A menu, not a boolean, and 0 and 2 do not exist. |
| `exposure_time_absolute` | 1–5000 | Units of 100 µs. Inactive under aperture priority. |
| `gain` | 0–100 | Reads as a live measurement under automatic exposure. |
| `white_balance_automatic` | 0/1 | |
| `white_balance_temperature` | 2300–6500 K | Inactive while automatic. |
| `brightness` | −64–191 | |
| `contrast` | 0–255 | |
| `saturation` | 0–128 | |
| `hue` | −40–40 | |
| `gamma` | 72–500 | |
| `sharpness` | 1–128 | |
| `backlight_compensation` | 0/1 | |
| `power_line_frequency` | 0/1/2 (disabled / 50 Hz / 60 Hz) | |
| `zoom_continuous` | 0–0 | Present in the control list, but its range is empty: nothing to set. |

### What it does not have

Not a matter of the driver missing something — the Camera Terminal's
`bmControls` bitmap is `0x0002266a`, and the bits are simply clear:

- **No pan or tilt** (D11, D12). There is no way to aim a zoomed view; it is
  centred and stays centred. Cropping a wider frame is the only way to look at
  something off-centre, which is why `grab_frame` takes a `region`.
- **No privacy control** (D18) — the shutter, if any, is mechanical.
- **No region of interest** (D21), so the camera cannot be asked to meter or
  focus on part of the frame.

Roll (D13) *is* claimed in the bitmap, but `uvcvideo` maps no V4L2 control onto
it, so it is unreachable without raw UVC requests.

## Capture formats

MJPEG at 3840×2160/30, 2560×1440/30, 1920×1080/60, 1280×960/30, 1280×720/60,
1024×576/60, 960×720/30, 800×600/30, 640×480/30, 640×360/60. YUYV only at
640×480 and 640×360.

The camera is not the limit on frame rate in this setup — cam2ip is. It decodes
every frame to an `image.Image` and re-encodes it (`handlers/stream.go` does
both unconditionally; the timestamp overlay is *not* what costs), in one
goroutine. Measured on the deployment host, 6 cores, 2026-07-30:

| Capture | Delivered | CPU | Per frame |
|---|---|---|---|
| 1280×720 | 27.4 fps | 0.74 core | 63 KB |
| 1920×1080 | 14.8 fps | 0.86 core | 121 KB |
| 2560×1440 | 8.2 fps | 0.92 core | 278 KB |
| 3840×2160 | 3.9 fps | 0.97 core | 554 KB |

1080p is the deployed setting: four times the pixels of 720p, still fast enough
that the five warmup frames the freshness logic drops cost 0.34 s. 4K costs a
saturated core and a 1.3 s warmup to recover detail that `camera_zoom` and
`grab_frame(region=...)` recover on demand and only when asked.

## The vendor Extension Unit

The descriptors advertise one, and it looks promising:

```
bDescriptorSubtype  6 (EXTENSION_UNIT)
bUnitID             2
guidExtensionCode   {46394292-0cd0-4ae3-8783-3133f9eaaa3b}
bNumControls        10
```

Ten selectors, all reporting `GET,SET`. This is where a camera of this class
keeps its AI auto-framing and its HDR mode, so it is the obvious place to look
for the features the box advertises and the V4L2 control list does not have.

`tools/uvc_xu_probe.py` dumps it. Reading is safe and finds:

| Selector | Length | Current value |
|---|---|---|
| 1, 2, 4, 7, 8 | 1 byte | small integers |
| 3 | 2 bytes | `0001` |
| 5 | 10 bytes | structured |
| 6, 9 | 1024 bytes | `0304` then zeros |
| 10 | 12 bytes | `27000000000000000000 00d8` |

**`min`, `max` and `def` are filler.** Every selector reports `min=00…`,
`max=ff…`, `def=ff…`, `res=01…` regardless of length. The unit does not
implement those queries, so there is no self-description to work from: the
device will not say what any of these mean or what values are legal.

### Why this was not turned into a feature, and what probing it showed

It was probed with writes — bounded to the 1- and 2-byte selectors, each write
preceded by a read and followed by a restore and a verify, with the 1024-byte
buffers (6 and 9) never written on the grounds that a malformed command is the
one thing here that might persist. Results:

1. **The values move on their own.** Selector 1 read `03` with the camera idle
   and `01` while streaming; selector 5 read `03000000…` idle and
   `0a0001030a8b02000000` streaming, changing again to `000101030a8b02000000`
   afterwards. These track the state of the pipeline.
2. **Writes that stick change nothing visible.** Selectors 1, 2, 4, 7 and 8 were
   each set to several values with the stream live and frames compared against a
   baseline. Every difference stayed at the noise floor of two untouched frames
   (~1.0 of 255, threshold 3.0). Selector 2 did not even take the write — it
   read back `00` each time.
3. **Selector 3 is not a register.** Written `01`, it read back `0876`;
   restoring `0001` left it reading `0876`, which tripped the abort. It returned
   to `0001` by itself a few seconds later. It is a computed readout, not a
   setting.

So this is a telemetry and command interface, not a bank of feature toggles. The
1-byte selectors that look like mode enums are status registers, and whatever
drives auto-framing is behind the 1024-byte command channel at selectors 6 and 9
— a protocol, not a value, and not something to guess at. Reaching it needs a
USB capture of EMEET's own application to supply the command format. Until
someone has that, this is a dead end, and the standard controls above are the
whole usable surface.

The camera was verified healthy afterwards: all selectors back at their original
values, every V4L2 control at its default, and a normal image (channel means
103/85/101, stddev 59/75/66).

## Microphone

The camera also registers as an ALSA capture device — a separate device sharing
the cable, so recording neither needs nor disturbs the video stream.

```
EMEET SmartCam S600 : USB Audio
  Format: S16_LE, Channels: 2 (FL FR), Rates: 48000, 32000, 16000
```

`record_audio` captures from it, but only when `AUDIO_CAPTURE` is switched on:
unlike the camera, a microphone advertises nothing while it is recording, and
the sound card merely being visible is not a reason to offer the room up.
