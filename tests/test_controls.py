"""Camera control behaviour, against a fake driver that reproduces the awkward parts.

The properties under test are the ones that fail silently on real hardware:
a manual control written while its automatic counterpart is on is accepted and
ignored, and a driver clamps out-of-range values without saying so. Both look
like success from the caller's side, so both need a test that checks the camera
rather than the return code.
"""

from __future__ import annotations

import asyncio

import pytest

import v4l2_controls as v4l2
from cam2mcp_server import ControlSession
from fake_v4l2 import ControlSpec, FakeDriver, install
from v4l2_controls import CameraControlError, CameraControls


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def controls(driver, monkeypatch, tmp_path):
    """A CameraControls backed by the fake driver and a real, empty device file."""
    install(monkeypatch, driver)
    device = tmp_path / "video0"
    device.write_bytes(b"")
    return CameraControls(str(device))


class TestEnumeration:
    def test_finds_every_control_with_its_range(self, controls):
        by_name = {c["name"]: c for c in controls.get_all()}

        assert by_name["zoom_absolute"]["min"] == 0
        assert by_name["zoom_absolute"]["max"] == 100
        assert by_name["focus_absolute"]["default"] == 192
        assert by_name["white_balance_automatic"]["type"] == "boolean"

    def test_names_match_the_ones_v4l2_ctl_prints(self, controls):
        """The slug is the identifier someone will cross-check with v4l2-ctl.

        Two spellings of the same control would turn "did the server do what it
        said" into a puzzle, so this pins the mapping rather than trusting it.
        """
        names = {c["name"] for c in controls.get_all()}

        assert "focus_automatic_continuous" in names  # "Focus, Automatic Continuous"
        assert "white_balance_temperature" in names  # "White Balance Temperature"
        assert "auto_exposure" in names  # "Auto Exposure"

    def test_menu_lists_only_the_values_that_exist(self, controls):
        """auto_exposure spans 0..3 but only 1 and 3 are real.

        Reporting the range instead of the values would invite a caller to try 2
        and get an error the control list said was fine.
        """
        auto_exposure = next(
            c for c in controls.get_all() if c["name"] == "auto_exposure"
        )

        assert auto_exposure["options"] == {"1": "Manual Mode", "3": "Aperture Priority Mode"}
        assert auto_exposure["value_label"] == "Aperture Priority Mode"

    def test_marks_controls_an_automatic_mode_is_overriding(self, controls):
        listed = {c["name"]: c for c in controls.get_all()}
        assert listed["focus_absolute"]["inactive"] is True

        controls.set("focus_automatic_continuous", 0)

        relisted = {c["name"]: c for c in controls.get_all()}
        assert "inactive" not in relisted["focus_absolute"]


class TestSetting:
    def test_turns_off_the_automatic_mode_that_would_swallow_the_write(
        self, controls, driver
    ):
        assert driver.value_of(v4l2.CID_FOCUS_AUTO) == 1

        controls.set("focus_absolute", 800)

        assert driver.value_of(v4l2.CID_FOCUS_AUTO) == 0
        assert driver.value_of(v4l2.CID_FOCUS_ABSOLUTE) == 800

    def test_switches_exposure_to_manual_for_an_exposure_time(self, controls, driver):
        controls.set("exposure_time_absolute", 900)

        assert driver.value_of(v4l2.CID_EXPOSURE_AUTO) == v4l2.EXPOSURE_MANUAL
        assert driver.value_of(v4l2.CID_EXPOSURE_ABSOLUTE) == 900

    def test_reports_what_the_driver_stored_not_what_was_asked(self, monkeypatch, tmp_path):
        """A clamping driver is the normal case, and it does not report clamping.

        Echoing the request back would leave a caller reasoning about a zoom
        level the camera never reached.
        """
        driver = FakeDriver(clamp=True)
        install(monkeypatch, driver)
        device = tmp_path / "video0"
        device.write_bytes(b"")
        controls = CameraControls(str(device))
        # Bypass this module's own range check to get at the driver's behaviour.
        stored = controls.set(v4l2.CID_ZOOM_ABSOLUTE, 100)

        assert stored == 100

    def test_refuses_a_value_outside_the_range(self, controls):
        with pytest.raises(CameraControlError, match=r"outside 0\.\.100"):
            controls.set("zoom_absolute", 500)

    def test_refuses_a_menu_value_that_does_not_exist(self, controls):
        with pytest.raises(CameraControlError, match="not one of its values"):
            controls.set("auto_exposure", 2)

    def test_unknown_control_names_the_ones_that_exist(self, controls):
        with pytest.raises(CameraControlError, match="zoom_absolute"):
            controls.set("zoom", 10)


class TestRestore:
    def test_puts_back_only_what_it_changed(self, controls, driver):
        driver.controls[v4l2.CID_SHARPNESS].value = 64  # somebody else's setting

        controls.set("zoom_absolute", 40)
        controls.restore()

        assert driver.value_of(v4l2.CID_ZOOM_ABSOLUTE) == 0
        assert driver.value_of(v4l2.CID_SHARPNESS) == 64

    def test_remembers_the_value_from_before_the_first_write(self, controls, driver):
        controls.set("zoom_absolute", 40)
        controls.set("zoom_absolute", 70)
        controls.set("zoom_absolute", 90)

        controls.restore()

        assert driver.value_of(v4l2.CID_ZOOM_ABSOLUTE) == 0

    def test_restores_the_automatic_mode_it_switched_off(self, controls, driver):
        controls.set("focus_absolute", 800)
        controls.restore()

        assert driver.value_of(v4l2.CID_FOCUS_AUTO) == 1
        assert driver.value_of(v4l2.CID_FOCUS_ABSOLUTE) == 192

    def test_writes_the_manual_value_before_re_enabling_the_automatic_mode(
        self, controls, driver
    ):
        """Order is the whole test. Autofocus back on first makes the focus
        position inactive, so the position write lands nowhere and the camera
        stays where the caller left it -- while restore() reports success."""
        controls.set("focus_absolute", 800)
        driver.writes.clear()

        controls.restore()

        order = driver.write_order()
        assert order.index(v4l2.CID_FOCUS_ABSOLUTE) < order.index(v4l2.CID_FOCUS_AUTO)
        assert driver.value_of(v4l2.CID_FOCUS_ABSOLUTE) == 192

    def test_is_empty_and_harmless_when_nothing_was_changed(self, controls, driver):
        driver.writes.clear()

        assert controls.restore() == {}
        assert driver.writes == []

    def test_a_second_restore_does_nothing(self, controls, driver):
        controls.set("zoom_absolute", 40)
        controls.restore()
        driver.writes.clear()

        assert controls.restore() == {}
        assert driver.writes == []

    def test_reset_to_defaults_clears_state_this_server_did_not_set(self, controls, driver):
        driver.controls[v4l2.CID_SHARPNESS].value = 64
        driver.controls[v4l2.CID_ZOOM_ABSOLUTE].value = 80

        controls.reset_to_defaults()

        assert driver.value_of(v4l2.CID_SHARPNESS) == 32
        assert driver.value_of(v4l2.CID_ZOOM_ABSOLUTE) == 0

    def test_reset_to_defaults_orders_automatic_modes_last_too(self, controls, driver):
        driver.controls[v4l2.CID_FOCUS_AUTO].value = 0
        driver.controls[v4l2.CID_FOCUS_ABSOLUTE].value = 900

        controls.reset_to_defaults()

        assert driver.value_of(v4l2.CID_FOCUS_ABSOLUTE) == 192
        assert driver.value_of(v4l2.CID_FOCUS_AUTO) == 1


class TestSession:
    """The async wrapper: idle restore, and what defers it."""

    @pytest.fixture
    def session(self, controls):
        return ControlSession(controls, idle_s=0.3)

    async def test_restores_once_the_camera_goes_idle(self, session, driver):
        await session.set_many({"zoom_absolute": 40})
        assert driver.value_of(v4l2.CID_ZOOM_ABSOLUTE) == 40

        await asyncio.sleep(0.6)

        assert driver.value_of(v4l2.CID_ZOOM_ABSOLUTE) == 0

    async def test_activity_defers_the_restore(self, session, driver):
        """A caller still taking pictures is still using the settings it chose."""
        await session.set_many({"zoom_absolute": 40})

        for _ in range(4):
            await asyncio.sleep(0.15)
            session.touch()

        assert driver.value_of(v4l2.CID_ZOOM_ABSOLUTE) == 40

        await asyncio.sleep(0.6)
        assert driver.value_of(v4l2.CID_ZOOM_ABSOLUTE) == 0

    async def test_a_zero_idle_setting_never_restores_automatically(self, controls, driver):
        session = ControlSession(controls, idle_s=0.0)
        await session.set_many({"zoom_absolute": 40})

        await asyncio.sleep(0.4)

        assert driver.value_of(v4l2.CID_ZOOM_ABSOLUTE) == 40
        await session.restore()
        assert driver.value_of(v4l2.CID_ZOOM_ABSOLUTE) == 0

    async def test_restores_on_shutdown(self, session, driver):
        await session.set_many({"zoom_absolute": 40})

        await session.aclose()

        assert driver.value_of(v4l2.CID_ZOOM_ABSOLUTE) == 0

    async def test_set_many_applies_in_order_and_reports_each(self, session):
        applied = await session.set_many({"zoom_absolute": 40, "sharpness": 96})

        assert applied == {"zoom_absolute": 40, "sharpness": 96}

    async def test_a_failure_part_way_says_what_already_landed(self, session, driver):
        """The applied ones are real changes to the camera; the error has to say
        so, or the caller is left guessing at the state it is now in."""
        with pytest.raises(CameraControlError, match="Already applied: zoom_absolute=40"):
            await session.set_many({"zoom_absolute": 40, "sharpness": 9999})

        assert driver.value_of(v4l2.CID_ZOOM_ABSOLUTE) == 40

    async def test_status_reports_what_is_outstanding(self, session):
        await session.set_many({"zoom_absolute": 40})

        assert session.status()["changed_by_this_server"] == {"zoom_absolute": 0}

        await session.restore()
        assert session.status()["changed_by_this_server"] is None


class TestMissingDevice:
    def test_says_what_to_do_about_a_missing_node(self, tmp_path):
        controls = CameraControls(str(tmp_path / "nope"))

        with pytest.raises(CameraControlError, match="does not exist"):
            controls.get_all()

    def test_a_driver_with_no_controls_is_not_an_error(self, monkeypatch, tmp_path):
        install(monkeypatch, FakeDriver(controls=[]))
        device = tmp_path / "video0"
        device.write_bytes(b"")

        assert CameraControls(str(device)).get_all() == []


class TestReadOnlyControls:
    def test_a_read_only_control_is_listed_but_refused(self, monkeypatch, tmp_path):
        spec = ControlSpec(
            v4l2.CID_GAIN, "Gain", v4l2.CTRL_TYPE_INTEGER, 0, 100, 0,
            flags=v4l2.FLAG_READ_ONLY,
        )
        install(monkeypatch, FakeDriver(controls=[spec]))
        device = tmp_path / "video0"
        device.write_bytes(b"")
        controls = CameraControls(str(device))

        listed = controls.get_all()
        assert listed[0]["writable"] is False

        with pytest.raises(CameraControlError, match="read-only"):
            controls.set("gain", 50)
