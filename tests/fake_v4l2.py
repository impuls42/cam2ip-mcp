"""A fake V4L2 control interface, modelled on the camera this was built against.

Only fcntl.ioctl is replaced. The device path is a real (empty) file, so
os.open and os.close do their normal work on a normal descriptor -- patching
those globally would reach every other thing the test process has open.

The control set mirrors an EMEET SmartCam S600 because the interesting
behaviour is not generic: manual controls that go inactive under an automatic
one, a menu whose valid values have a gap in them (auto_exposure offers 1 and 3
out of a 0..3 range), and a driver that clamps rather than refusing. A fake with
a tidy control set would pass while the real thing did none of that.
"""

from __future__ import annotations

import errno

import v4l2_controls as v4l2


class ControlSpec:
    def __init__(
        self,
        cid: int,
        name: str,
        ctype: int,
        minimum: int,
        maximum: int,
        default: int,
        value: int | None = None,
        menu: dict[int, str] | None = None,
        flags: int = 0,
        # The automatic control that, while set to `when`, makes this one
        # inactive -- the relationship the real driver reports through
        # V4L2_CTRL_FLAG_INACTIVE and that the auto-pairing has to work around.
        inactive_under: tuple[int, int] | None = None,
    ) -> None:
        self.id = cid
        self.name = name
        self.type = ctype
        self.minimum = minimum
        self.maximum = maximum
        self.default = default
        self.value = default if value is None else value
        self.menu = menu or {}
        self.flags = flags
        self.inactive_under = inactive_under


def s600_controls() -> list[ControlSpec]:
    I, B, M = v4l2.CTRL_TYPE_INTEGER, v4l2.CTRL_TYPE_BOOLEAN, v4l2.CTRL_TYPE_MENU
    return [
        ControlSpec(v4l2.CID_BRIGHTNESS, "Brightness", I, -64, 191, 0),
        ControlSpec(v4l2.CID_CONTRAST, "Contrast", I, 0, 255, 57),
        ControlSpec(v4l2.CID_SATURATION, "Saturation", I, 0, 128, 82),
        ControlSpec(v4l2.CID_AUTO_WHITE_BALANCE, "White Balance, Automatic", B, 0, 1, 1),
        ControlSpec(v4l2.CID_GAIN, "Gain", I, 0, 100, 0),
        ControlSpec(
            v4l2.CID_WHITE_BALANCE_TEMPERATURE, "White Balance Temperature",
            I, 2300, 6500, 5000,
            inactive_under=(v4l2.CID_AUTO_WHITE_BALANCE, 1),
        ),
        ControlSpec(v4l2.CID_SHARPNESS, "Sharpness", I, 1, 128, 32),
        ControlSpec(
            v4l2.CID_EXPOSURE_AUTO, "Auto Exposure", M, 0, 3, 3,
            menu={1: "Manual Mode", 3: "Aperture Priority Mode"},
        ),
        ControlSpec(
            v4l2.CID_EXPOSURE_ABSOLUTE, "Exposure Time, Absolute", I, 1, 5000, 300,
            inactive_under=(v4l2.CID_EXPOSURE_AUTO, v4l2.EXPOSURE_AUTO),
        ),
        ControlSpec(
            v4l2.CID_FOCUS_ABSOLUTE, "Focus, Absolute", I, 0, 1023, 192,
            inactive_under=(v4l2.CID_FOCUS_AUTO, 1),
        ),
        ControlSpec(v4l2.CID_FOCUS_AUTO, "Focus, Automatic Continuous", B, 0, 1, 1),
        ControlSpec(v4l2.CID_ZOOM_ABSOLUTE, "Zoom, Absolute", I, 0, 100, 0),
    ]


class FakeDriver:
    """Answers the four control ioctls this project issues, and counts them."""

    def __init__(self, controls: list[ControlSpec] | None = None, clamp: bool = True) -> None:
        specs = s600_controls() if controls is None else controls
        self.controls = {spec.id: spec for spec in specs}
        # Real drivers hand controls back in ascending id order, and enumeration
        # depends on that -- NEXT_CTRL means "the next one up", not "another one".
        self.order = sorted(self.controls)
        self.clamp = clamp
        self.writes: list[tuple[int, int]] = []

    # -- helpers -----------------------------------------------------------

    def _flags(self, spec: ControlSpec) -> int:
        flags = spec.flags
        if spec.inactive_under is not None:
            auto_id, auto_value = spec.inactive_under
            auto = self.controls.get(auto_id)
            if auto is not None and auto.value == auto_value:
                flags |= v4l2.FLAG_INACTIVE
        return flags

    def value_of(self, cid: int) -> int:
        return self.controls[cid].value

    def write_order(self) -> list[int]:
        return [cid for cid, _ in self.writes]

    # -- the ioctl ---------------------------------------------------------

    def ioctl(self, fd: int, request: int, arg):
        if request == v4l2.VIDIOC_QUERYCTRL:
            return self._queryctrl(arg)
        if request == v4l2.VIDIOC_QUERYMENU:
            return self._querymenu(arg)
        if request == v4l2.VIDIOC_G_CTRL:
            return self._get(arg)
        if request == v4l2.VIDIOC_S_CTRL:
            return self._set(arg)
        raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")

    def _queryctrl(self, arg):
        wanted = arg.id
        if wanted & v4l2.V4L2_CTRL_FLAG_NEXT_CTRL:
            base = wanted & ~v4l2.V4L2_CTRL_FLAG_NEXT_CTRL
            nxt = next((cid for cid in self.order if cid > base), None)
            if nxt is None:
                raise OSError(errno.EINVAL, "Invalid argument")
            spec = self.controls[nxt]
        else:
            if wanted not in self.controls:
                raise OSError(errno.EINVAL, "Invalid argument")
            spec = self.controls[wanted]

        arg.id = spec.id
        arg.type = spec.type
        arg.name = spec.name.encode()
        arg.minimum = spec.minimum
        arg.maximum = spec.maximum
        arg.step = 1
        arg.default_value = spec.default
        arg.flags = self._flags(spec)
        return arg

    def _querymenu(self, arg):
        spec = self.controls.get(arg.id)
        if spec is None or arg.index not in spec.menu:
            raise OSError(errno.EINVAL, "Invalid argument")
        arg.name = spec.menu[arg.index].encode()
        return arg

    def _get(self, arg):
        spec = self.controls.get(arg.id)
        if spec is None:
            raise OSError(errno.EINVAL, "Invalid argument")
        arg.value = spec.value
        return arg

    def _set(self, arg):
        spec = self.controls.get(arg.id)
        if spec is None:
            raise OSError(errno.EINVAL, "Invalid argument")
        value = arg.value
        if spec.menu and value not in spec.menu:
            raise OSError(errno.EINVAL, "Invalid argument")
        if not spec.menu:
            if self.clamp:
                value = max(spec.minimum, min(spec.maximum, value))
            elif not spec.minimum <= value <= spec.maximum:
                raise OSError(errno.ERANGE, "Numerical result out of range")

        # A write to an inactive control is accepted and stored but does not
        # take effect -- which is precisely what makes restoring in the wrong
        # order fail silently, so the fake has to reproduce it rather than
        # helpfully rejecting the write.
        if self._flags(spec) & v4l2.FLAG_INACTIVE:
            self.writes.append((spec.id, value))
            return arg

        spec.value = value
        self.writes.append((spec.id, value))
        return arg


class _FcntlShim:
    """Stands in for the fcntl module inside v4l2_controls, and nowhere else.

    Patching fcntl.ioctl on the real module would reach every other thing in the
    test process that issues an ioctl. Rebinding the *name* in v4l2_controls'
    namespace is the same substitution with none of the reach.
    """

    def __init__(self, driver: "FakeDriver") -> None:
        self.ioctl = driver.ioctl


def install(monkeypatch, driver: FakeDriver) -> None:
    monkeypatch.setattr(v4l2, "fcntl", _FcntlShim(driver))
