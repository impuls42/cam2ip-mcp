#!/usr/bin/env python3
"""Dump a UVC Extension Unit's control surface. Read-only, by construction.

An Extension Unit is where a vendor puts whatever the UVC standard has no
control for -- on a webcam that usually means the "AI" features the box
advertises. The unit and its controls appear in the USB descriptors
(`lsusb -v`, look for EXTENSION_UNIT), but the descriptors give only a GUID and
a count: what each selector *means* is documented nowhere the device can be
asked. This prints what each one is, how long it is, and what it currently
holds, which is the starting point for working the rest out from a USB capture
of the vendor's own application.

Only GET queries are issued. There is no code path here that writes, and that is
deliberate rather than incidental -- see docs/emeet-s600.md for what happened
when this was probed with writes, and why the interesting-looking selectors
turned out not to be settings at all.

Usage:
    ./uvc_xu_probe.py /dev/video0            # unit 2, selectors 1..16
    ./uvc_xu_probe.py /dev/video0 --unit 3 --max-selector 32
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import os
import sys

# _IOWR('u', 0x21, struct uvc_xu_control_query). The struct is
# {u8 unit; u8 selector; u8 query; u16 size; u8 *data;}, which pads to 16 bytes
# on a 64-bit kernel: the u16 aligns to offset 4 and the pointer to offset 8.
UVCIOC_CTRL_QUERY = 0xC0107521

UVC_GET_CUR = 0x81
UVC_GET_MIN = 0x82
UVC_GET_MAX = 0x83
UVC_GET_RES = 0x84
UVC_GET_LEN = 0x85
UVC_GET_INFO = 0x86
UVC_GET_DEF = 0x87

QUERY_NAMES = {
    UVC_GET_CUR: "cur",
    UVC_GET_MIN: "min",
    UVC_GET_MAX: "max",
    UVC_GET_DEF: "def",
    UVC_GET_RES: "res",
}


class XuControlQuery(ctypes.Structure):
    _fields_ = [
        ("unit", ctypes.c_uint8),
        ("selector", ctypes.c_uint8),
        ("query", ctypes.c_uint8),
        ("size", ctypes.c_uint16),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
    ]


def query(fd: int, unit: int, selector: int, request: int, size: int) -> bytes:
    buffer = (ctypes.c_uint8 * max(size, 1))()
    arg = XuControlQuery(
        unit, selector, request, size,
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint8)),
    )
    fcntl.ioctl(fd, UVCIOC_CTRL_QUERY, arg)
    return bytes(buffer)[:size]


def describe_info(flags: int) -> str:
    names = [
        (0x01, "GET"), (0x02, "SET"), (0x04, "disabled"),
        (0x08, "autoupdate"), (0x10, "async"),
    ]
    return ",".join(name for bit, name in names if flags & bit) or "none"


def abbreviate(payload: bytes, limit: int = 32) -> str:
    """Hex, truncated. A 1024-byte buffer of zeros tells you nothing at length."""
    head = payload[:limit].hex()
    return head + ("..." if len(payload) > limit else "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("device", help="V4L2 node, e.g. /dev/video0")
    parser.add_argument("--unit", type=int, default=2,
                        help="extension unit id from the descriptors (default 2)")
    parser.add_argument("--max-selector", type=int, default=16,
                        help="highest selector to try (default 16)")
    args = parser.parse_args()

    try:
        # Read-write because the UVC driver requires it before it will pass any
        # control query through, including a GET. Nothing below writes.
        fd = os.open(args.device, os.O_RDWR)
    except OSError as exc:
        print(f"cannot open {args.device}: {exc.strerror}", file=sys.stderr)
        return 1

    print(f"{args.device}, extension unit {args.unit}\n")
    found = 0
    try:
        for selector in range(1, args.max_selector + 1):
            try:
                length = int.from_bytes(query(fd, args.unit, selector, UVC_GET_LEN, 2), "little")
            except OSError as exc:
                print(f"  sel {selector:2d}: absent ({exc.strerror})")
                continue
            if length == 0:
                print(f"  sel {selector:2d}: absent (zero length)")
                continue

            found += 1
            try:
                info = describe_info(query(fd, args.unit, selector, UVC_GET_INFO, 1)[0])
            except OSError as exc:
                info = f"unreadable ({exc.strerror})"

            print(f"  sel {selector:2d}: {length} byte(s), info[{info}]")
            for request in (UVC_GET_CUR, UVC_GET_MIN, UVC_GET_MAX, UVC_GET_DEF, UVC_GET_RES):
                try:
                    value = query(fd, args.unit, selector, request, length)
                except OSError:
                    continue
                print(f"        {QUERY_NAMES[request]}: {abbreviate(value)}")
    finally:
        os.close(fd)

    if not found:
        print("no selectors answered. Check the unit id against `lsusb -v`: the "
              "EXTENSION_UNIT descriptor's bUnitID is the number to pass to --unit.")
        return 1

    print(f"\n{found} selector(s) present. Note that min/max/def are often filler "
          f"(all 0xff): most vendor units do not implement them, so a range "
          f"printed above is not evidence that the values in it are valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
