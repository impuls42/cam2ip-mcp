#!/usr/bin/env python3
"""Read and write V4L2 camera controls on a device that something else is streaming.

Why this can run alongside cam2ip
---------------------------------
V4L2 separates the streaming interface from the control interface. Buffers,
formats and STREAMON are exclusive -- one process negotiates them and everyone
else gets EBUSY -- but VIDIOC_G_CTRL and VIDIOC_S_CTRL are not part of that
bargain. Any process that can open the node can read and write controls, and the
change lands in the camera's firmware where it affects whatever capture is
already in flight.

That is exactly the property this module needs. cam2ip owns the stream and
exposes no control surface of its own; this opens the same node beside it and
drives the controls directly. Nothing here calls VIDIOC_S_FMT, VIDIOC_REQBUFS or
VIDIOC_STREAMON, so nothing here can take the stream away from cam2ip -- and the
file descriptor is opened per operation rather than held, so an idle server is
not sitting on the device.

Two consequences of talking straight to the firmware are worth stating, because
callers have to design around them rather than ignore them:

  * Control state is global to the camera, not to this process. It survives this
    process exiting, cam2ip restarting, and the container being replaced. Only
    unplugging the camera (or writing the value back) clears it. Whoever picks
    the camera up next -- a video call, say -- inherits whatever was left set.
  * Some controls deactivate others. Autofocus being on makes the manual focus
    position inactive; the driver reports that with V4L2_CTRL_FLAG_INACTIVE and
    the value written to an inactive control does not take effect. Setting a
    manual value therefore has to turn its automatic counterpart off first, which
    is what AUTO_PAIRS below encodes.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# ioctl plumbing
# ---------------------------------------------------------------------------

# _IOWR(type, nr, size) as the kernel builds it: direction in the top two bits,
# then the size of the argument struct, the ioctl "type" letter, and the number.
# Spelling it out beats hardcoding the constants, because the struct sizes below
# are the part that would silently drift.
_IOC_WRITE, _IOC_READ = 1, 2


def _iowr(type_letter: str, nr: int, size: int) -> int:
    return (
        ((_IOC_READ | _IOC_WRITE) << 30)
        | (size << 16)
        | (ord(type_letter) << 8)
        | nr
    )


class _Control(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("value", ctypes.c_int32)]


class _QueryCtrl(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("name", ctypes.c_char * 32),
        ("minimum", ctypes.c_int32),
        ("maximum", ctypes.c_int32),
        ("step", ctypes.c_int32),
        ("default_value", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 2),
    ]


class _QueryMenu(ctypes.Structure):
    # Packed in the kernel headers, and it matters: without it ctypes would pad
    # the 4-byte trailing field out to the union's 8-byte alignment and the
    # struct would be 48 bytes against the 44 the ioctl number encodes.
    _pack_ = 1
    _fields_ = [
        ("id", ctypes.c_uint32),
        ("index", ctypes.c_uint32),
        ("name", ctypes.c_char * 32),
        ("reserved", ctypes.c_uint32),
    ]


VIDIOC_G_CTRL = _iowr("V", 27, ctypes.sizeof(_Control))
VIDIOC_S_CTRL = _iowr("V", 28, ctypes.sizeof(_Control))
VIDIOC_QUERYCTRL = _iowr("V", 36, ctypes.sizeof(_QueryCtrl))
VIDIOC_QUERYMENU = _iowr("V", 37, ctypes.sizeof(_QueryMenu))

# Passed in the id to mean "the next control after this one", which is how a
# driver's control list is walked without knowing the ids up front. It is also
# what makes enumeration skip the gaps in the id space instead of probing 65k
# integers to find a dozen controls.
V4L2_CTRL_FLAG_NEXT_CTRL = 0x80000000

CTRL_TYPE_INTEGER = 1
CTRL_TYPE_BOOLEAN = 2
CTRL_TYPE_MENU = 3
CTRL_TYPE_BUTTON = 4
CTRL_TYPE_INTEGER_MENU = 9

TYPE_NAMES = {
    CTRL_TYPE_INTEGER: "integer",
    CTRL_TYPE_BOOLEAN: "boolean",
    CTRL_TYPE_MENU: "menu",
    CTRL_TYPE_BUTTON: "button",
    5: "integer64",
    6: "control-class",
    7: "string",
    8: "bitmask",
    CTRL_TYPE_INTEGER_MENU: "integer-menu",
}

FLAG_DISABLED = 0x0001
FLAG_READ_ONLY = 0x0004
FLAG_INACTIVE = 0x0010
FLAG_WRITE_ONLY = 0x0040
FLAG_VOLATILE = 0x0080

# Controls this module knows by name rather than by discovery. Everything else
# still enumerates and still works; these are the ones the dedicated tools and
# the automatic/manual pairing need to refer to explicitly.
CID_BRIGHTNESS = 0x00980900
CID_CONTRAST = 0x00980901
CID_SATURATION = 0x00980902
CID_AUTO_WHITE_BALANCE = 0x0098090C
CID_GAIN = 0x00980913
CID_WHITE_BALANCE_TEMPERATURE = 0x0098091A
CID_SHARPNESS = 0x0098091B
CID_BACKLIGHT_COMPENSATION = 0x0098091C

CID_EXPOSURE_AUTO = 0x009A0901
CID_EXPOSURE_ABSOLUTE = 0x009A0902
CID_FOCUS_ABSOLUTE = 0x009A090A
CID_FOCUS_AUTO = 0x009A090C
CID_ZOOM_ABSOLUTE = 0x009A090D

# V4L2_CID_EXPOSURE_AUTO is a menu, not a boolean, and its values are not a
# 0=off/1=on pair: 1 is manual and 3 is the "camera picks" mode this hardware
# defaults to. Naming them stops the tools from hardcoding bare integers.
EXPOSURE_MANUAL = 1
EXPOSURE_AUTO = 3

# manual control -> the automatic control that deactivates it. Writing the
# manual value requires switching the automatic one off first; restoring the
# manual value requires doing it *before* switching the automatic one back on,
# which is why restore() orders its writes the way it does.
AUTO_PAIRS = {
    CID_FOCUS_ABSOLUTE: (CID_FOCUS_AUTO, 0),
    CID_EXPOSURE_ABSOLUTE: (CID_EXPOSURE_AUTO, EXPOSURE_MANUAL),
    CID_WHITE_BALANCE_TEMPERATURE: (CID_AUTO_WHITE_BALANCE, 0),
}

AUTO_CIDS = frozenset(auto for auto, _ in AUTO_PAIRS.values())


class CameraControlError(RuntimeError):
    """A control could not be read or written."""


@dataclass(frozen=True)
class Control:
    id: int
    name: str
    slug: str
    type: int
    minimum: int
    maximum: int
    step: int
    default: int
    flags: int
    menu: dict[int, str] = field(default_factory=dict)

    @property
    def writable(self) -> bool:
        return not self.flags & (FLAG_READ_ONLY | FLAG_DISABLED)

    @property
    def inactive(self) -> bool:
        """Whether an automatic mode is currently overriding this control.

        Not a permanent property: it flips as its automatic counterpart is
        switched. A value written while this is set does not take effect, and the
        driver does not report an error for it.
        """
        return bool(self.flags & FLAG_INACTIVE)

    def describe(self, value: int | None) -> dict[str, object]:
        out: dict[str, object] = {
            "name": self.slug,
            "label": self.name,
            "type": TYPE_NAMES.get(self.type, str(self.type)),
            "value": value,
        }
        if self.type in (CTRL_TYPE_INTEGER, CTRL_TYPE_INTEGER_MENU):
            out["min"], out["max"], out["step"] = self.minimum, self.maximum, self.step
        if self.type != CTRL_TYPE_BUTTON:
            out["default"] = self.default
        if self.menu:
            out["options"] = {str(k): v for k, v in sorted(self.menu.items())}
            if value is not None and value in self.menu:
                out["value_label"] = self.menu[value]
        if self.inactive:
            out["inactive"] = True
            out["inactive_reason"] = "an automatic mode is overriding this control"
        if not self.writable:
            out["writable"] = False
        return out


def _slugify(name: str) -> str:
    """Turn a driver's display name into the identifier v4l2-ctl uses.

    "Zoom, Absolute" -> "zoom_absolute", "White Balance, Automatic" ->
    "white_balance_automatic". Matching v4l2-ctl matters more than elegance here:
    it is the tool anyone will reach for to check what this server did, and two
    different spellings of the same control would make that comparison a puzzle.
    """
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", name.lower())).strip("_")


class CameraControls:
    """The control surface of one V4L2 device, with undo for what it changed.

    Undo is deliberately narrow: only controls this object actually wrote are
    remembered, and only those are put back. Snapshotting everything and
    restoring it wholesale would look tidier and behave worse -- it would write
    back volatile controls whose reported value is a live measurement rather than
    a setting (gain under automatic exposure reads as whatever the sensor settled
    on), and pinning those to a stale reading is a change, not a restoration.
    """

    def __init__(self, device: str) -> None:
        self.device = device
        self._catalog: dict[int, Control] | None = None
        self._original: dict[int, int] = {}

    # -- device access -----------------------------------------------------

    def _open(self) -> int:
        """Open the node for one operation.

        O_NONBLOCK because opening a V4L2 device can otherwise wait on a driver
        that is busy, and there is no reason for a control write to inherit the
        stream's problems. The descriptor is not kept: control traffic is rare,
        opening costs microseconds, and not holding the node means an idle server
        has no claim on the camera at all.
        """
        try:
            return os.open(self.device, os.O_RDWR | os.O_NONBLOCK)
        except FileNotFoundError:
            raise CameraControlError(
                f"{self.device} does not exist. Set CAMERA_DEVICE to the capture "
                f"node, and pass it into the container (--device=/dev/video0)"
            ) from None
        except PermissionError:
            raise CameraControlError(
                f"no permission to open {self.device}; controls need read-write "
                f"access, and the process must be in the 'video' group or root"
            ) from None
        except OSError as exc:
            raise CameraControlError(f"cannot open {self.device}: {exc.strerror}") from None

    def _ioctl(self, fd: int, request: int, arg, what: str):
        try:
            fcntl.ioctl(fd, request, arg)
        except OSError as exc:
            raise CameraControlError(f"{what} failed: {exc.strerror}") from None
        return arg

    # -- enumeration -------------------------------------------------------

    def catalog(self, refresh: bool = False) -> dict[int, Control]:
        """Every control the driver exposes, keyed by id.

        Cached, because the set of controls and their ranges are fixed for a
        given device -- but the *flags* are not (INACTIVE moves as automatic
        modes are switched), so anything that reports flags to a caller passes
        refresh=True rather than trusting this.
        """
        if self._catalog is None or refresh:
            fd = self._open()
            try:
                self._catalog = self._enumerate(fd)
            finally:
                os.close(fd)
        return self._catalog

    def _enumerate(self, fd: int) -> dict[int, Control]:
        found: dict[int, Control] = {}
        query = _QueryCtrl()
        next_id = 0
        # A driver that does not implement NEXT_CTRL returns EINVAL on the very
        # first call, which is indistinguishable from an empty control list. That
        # is fine here -- uvcvideo has supported it since long before any camera
        # this runs against -- but it is why an empty catalog is reported as
        # "no controls" rather than treated as an error.
        while True:
            query.id = next_id | V4L2_CTRL_FLAG_NEXT_CTRL
            try:
                fcntl.ioctl(fd, VIDIOC_QUERYCTRL, query)
            except OSError as exc:
                if exc.errno == errno.EINVAL:
                    break  # walked off the end of the list
                raise CameraControlError(
                    f"enumerating controls on {self.device} failed: {exc.strerror}"
                ) from None

            next_id = query.id
            if query.flags & FLAG_DISABLED:
                continue
            # Control classes are headings in the id space, not settings.
            if query.type == 6:
                continue

            name = query.name.decode("utf-8", "replace")
            menu = {}
            if query.type in (CTRL_TYPE_MENU, CTRL_TYPE_INTEGER_MENU):
                menu = self._menu(fd, query)

            found[query.id] = Control(
                id=query.id,
                name=name,
                slug=_slugify(name),
                type=query.type,
                minimum=query.minimum,
                maximum=query.maximum,
                step=query.step or 1,
                default=query.default_value,
                flags=query.flags,
                menu=menu,
            )
        return found

    def _menu(self, fd: int, query: _QueryCtrl) -> dict[int, str]:
        """Labels for a menu control's valid values.

        The valid values are not the whole range: this camera's auto_exposure
        offers 1 and 3 out of a 0..3 span, and querying 0 and 2 returns EINVAL.
        Collecting only what answers is what lets a caller be told which values
        exist instead of discovering the gaps by getting an error.
        """
        items: dict[int, str] = {}
        menu = _QueryMenu()
        for index in range(query.minimum, query.maximum + 1):
            menu.id, menu.index = query.id, index
            try:
                fcntl.ioctl(fd, VIDIOC_QUERYMENU, menu)
            except OSError:
                continue
            if query.type == CTRL_TYPE_INTEGER_MENU:
                items[index] = str(int.from_bytes(menu.name[:8], "little", signed=True))
            else:
                items[index] = menu.name.decode("utf-8", "replace")
        return items

    def resolve(self, name: str | int) -> Control:
        """Find a control by slug, by driver name, or by numeric id."""
        catalog = self.catalog()
        if isinstance(name, int):
            if name not in catalog:
                raise CameraControlError(f"no control with id 0x{name:08x}")
            return catalog[name]

        wanted = _slugify(name)
        for control in catalog.values():
            if control.slug == wanted:
                return control
        known = ", ".join(sorted(c.slug for c in catalog.values()))
        raise CameraControlError(f"no control named {name!r}. Available: {known}")

    # -- get / set ---------------------------------------------------------

    def get(self, name: str | int) -> int:
        control = self.resolve(name)
        fd = self._open()
        try:
            arg = _Control(control.id, 0)
            self._ioctl(fd, VIDIOC_G_CTRL, arg, f"reading {control.slug}")
            return arg.value
        finally:
            os.close(fd)

    def get_all(self) -> list[dict[str, object]]:
        """Every control with its current value, ready to hand to a caller.

        Re-enumerates rather than using the cache, so the INACTIVE flags reflect
        the automatic modes as they are right now.
        """
        fd = self._open()
        try:
            catalog = self._enumerate(fd)
            self._catalog = catalog
            out = []
            for control in catalog.values():
                value: int | None = None
                if not control.flags & FLAG_WRITE_ONLY and control.type != CTRL_TYPE_BUTTON:
                    arg = _Control(control.id, 0)
                    try:
                        fcntl.ioctl(fd, VIDIOC_G_CTRL, arg)
                        value = arg.value
                    except OSError:
                        # An unreadable control is still worth listing with its
                        # range; refusing the whole call over one is worse.
                        value = None
                out.append(control.describe(value))
            return out
        finally:
            os.close(fd)

    def set(self, name: str | int, value: int, _remember: bool = True) -> int:
        """Write a control, and return what the driver actually stored.

        The return value is read back rather than echoed, because a driver is
        entitled to clamp to the range or round to the step and does so
        silently. Reporting the request as if it had been honoured is how a
        caller ends up reasoning about a zoom level the camera never reached.
        """
        control = self.resolve(name)

        if not control.writable:
            raise CameraControlError(f"{control.slug} is read-only")

        if control.type == CTRL_TYPE_BOOLEAN:
            value = 1 if value else 0
        elif control.menu and value not in control.menu:
            options = ", ".join(f"{k} ({v})" for k, v in sorted(control.menu.items()))
            raise CameraControlError(
                f"{control.slug}={value} is not one of its values: {options}"
            )
        elif control.type == CTRL_TYPE_INTEGER:
            if not control.minimum <= value <= control.maximum:
                raise CameraControlError(
                    f"{control.slug}={value} is outside {control.minimum}..{control.maximum}"
                )

        # Turn off whatever automatic mode would otherwise swallow this write.
        # Done before remembering the value below, so the automatic control's own
        # original value is recorded too and restore() puts both back.
        pair = AUTO_PAIRS.get(control.id)
        if pair is not None:
            auto_cid, manual_value = pair
            if auto_cid in self.catalog() and self.get(auto_cid) != manual_value:
                self.set(auto_cid, manual_value)

        fd = self._open()
        try:
            if _remember and control.id not in self._original:
                current = _Control(control.id, 0)
                try:
                    fcntl.ioctl(fd, VIDIOC_G_CTRL, current)
                    self._original[control.id] = current.value
                except OSError:
                    pass  # unreadable: nothing to restore it to, so do not pretend

            arg = _Control(control.id, value)
            try:
                fcntl.ioctl(fd, VIDIOC_S_CTRL, arg)
            except OSError as exc:
                if exc.errno == errno.ERANGE:
                    raise CameraControlError(
                        f"{control.slug}={value} was refused as out of range "
                        f"({control.minimum}..{control.maximum})"
                    ) from None
                raise CameraControlError(
                    f"setting {control.slug}={value} failed: {exc.strerror}"
                ) from None

            readback = _Control(control.id, 0)
            try:
                fcntl.ioctl(fd, VIDIOC_G_CTRL, readback)
                return readback.value
            except OSError:
                return value
        finally:
            os.close(fd)

    # -- undo --------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        return bool(self._original)

    def changed(self) -> dict[str, int]:
        """Controls written since the last restore, and the values to put back.

        Deliberately never opens the device. This is what camera_status reports,
        and status has to keep working when the controls do not -- a node that
        exists but cannot be opened (the process is not in the `video` group, say)
        must not stop the server describing a stream that is reaching the camera
        over HTTP and is perfectly healthy.

        It gets that for free rather than by catching anything. With nothing
        written there are no ids to name, so there is nothing to look up; and
        anything written means a set() already succeeded, which means it already
        enumerated and the catalog is cached. Either way the ioctl path is not
        reached.
        """
        if not self._original:
            return {}
        catalog = self._catalog or {}
        return {
            catalog[cid].slug if cid in catalog else f"0x{cid:08x}": value
            for cid, value in self._original.items()
        }

    def restore(self) -> dict[str, int]:
        """Put every control this object wrote back to the value it found.

        Order is load-bearing. Manual values go back first and automatic modes
        second, because an automatic mode that is already on makes its manual
        counterpart inactive -- restore autofocus before the focus position and
        the position write is quietly discarded, leaving the camera focused where
        the caller left it while this reports success.
        """
        if not self._original:
            return {}

        catalog = self.catalog()
        pending = sorted(
            self._original.items(),
            key=lambda item: item[0] in AUTO_CIDS,
        )

        restored: dict[str, int] = {}
        failed: list[str] = []
        for cid, value in pending:
            slug = catalog[cid].slug if cid in catalog else f"0x{cid:08x}"
            try:
                self.set(cid, value, _remember=False)
                restored[slug] = value
            except CameraControlError as exc:
                failed.append(f"{slug} ({exc})")

        # Cleared regardless: a control that cannot be written back now will not
        # become writable by being retried on every later restore, and keeping it
        # would make each subsequent restore replay the same failure.
        self._original.clear()

        if failed:
            raise CameraControlError(
                "restored " + (", ".join(restored) or "nothing")
                + "; could not restore " + ", ".join(failed)
            )
        return restored

    def reset_to_defaults(self) -> dict[str, int]:
        """Set every writable control to the driver's stated default.

        Distinct from restore(): that undoes this server's own changes, while
        this discards state from anywhere -- another application, an earlier
        container, a session that exited without restoring. Since UVC controls
        live in the camera rather than in any process, that history is otherwise
        invisible and only a reset clears it.
        """
        results: dict[str, int] = {}
        # Automatic modes last again, for the reason restore() explains: setting
        # them first would deactivate the manual controls before they are written.
        controls = sorted(
            self.catalog(refresh=True).values(),
            key=lambda c: c.id in AUTO_CIDS,
        )
        for control in controls:
            if not control.writable or control.type == CTRL_TYPE_BUTTON:
                continue
            try:
                results[control.slug] = self.set(control.id, control.default, _remember=False)
            except CameraControlError:
                continue
        self._original.clear()
        return results
