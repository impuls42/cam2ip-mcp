"""Region cropping: the geometry, and the errors that stop a bad region silently working."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from cam2mcp_server import CROP_QUALITY, crop_jpeg, frame_size


def jpeg(width: int, height: int, colour=(120, 30, 200)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="JPEG", quality=90)
    return out.getvalue()


def size_of(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size


class TestGeometry:
    def test_the_whole_frame_is_the_whole_frame(self):
        assert size_of(crop_jpeg(jpeg(1920, 1080), (0, 0, 1, 1), CROP_QUALITY)) == (1920, 1080)

    @pytest.mark.parametrize(
        "region, expected",
        [
            ((0.0, 0.0, 0.5, 0.5), (960, 540)),  # top left quarter
            ((0.5, 0.0, 0.5, 0.5), (960, 540)),  # top right quarter
            ((0.25, 0.25, 0.5, 0.5), (960, 540)),  # centred half
            ((0.0, 0.0, 1.0, 0.25), (1920, 270)),  # top strip
        ],
    )
    def test_fractions_map_to_pixels(self, region, expected):
        assert size_of(crop_jpeg(jpeg(1920, 1080), region, CROP_QUALITY)) == expected

    def test_the_region_is_measured_from_the_top_left(self):
        """x=0, y=0 is the top left. Getting this upside down would still return
        a plausible-looking crop of the wrong part of the scene, so it is checked
        against pixels rather than dimensions."""
        source = Image.new("RGB", (100, 100), (0, 0, 0))
        source.paste(Image.new("RGB", (50, 50), (255, 0, 0)), (50, 0))  # top right
        buffer = io.BytesIO()
        source.save(buffer, format="JPEG", quality=95)

        cropped = crop_jpeg(buffer.getvalue(), (0.5, 0.0, 0.5, 0.5), 95)

        with Image.open(io.BytesIO(cropped)) as image:
            red, green, blue = image.convert("RGB").getpixel((25, 25))
        assert red > 200 and green < 60 and blue < 60

    def test_survives_a_change_of_capture_resolution(self):
        """The point of fractions: the same region means the same part of the
        scene after the camera is reconfigured, which pixel coordinates would
        not."""
        region = (0.5, 0.5, 0.25, 0.25)

        assert size_of(crop_jpeg(jpeg(1280, 720), region, CROP_QUALITY)) == (320, 180)
        assert size_of(crop_jpeg(jpeg(3840, 2160), region, CROP_QUALITY)) == (960, 540)

    def test_a_tiny_region_still_yields_at_least_one_pixel(self):
        """Rounding a small fraction down to a zero-width box would make Pillow
        raise something opaque instead of returning the smallest honest answer."""
        width, height = size_of(crop_jpeg(jpeg(640, 480), (0.5, 0.5, 0.0001, 0.0001), 90))

        assert width >= 1 and height >= 1


class TestRejections:
    @pytest.mark.parametrize(
        "region, message",
        [
            ((-0.1, 0, 0.5, 0.5), "x=-0.1 must be between 0 and 1"),
            ((0, 1.5, 0.5, 0.5), "y=1.5 must be between 0 and 1"),
            ((0, 0, 0, 0.5), "greater than 0"),
            ((0, 0, 0.5, 0), "greater than 0"),
            ((0.7, 0, 0.5, 0.5), "runs past the edge"),
            ((0, 0.7, 0.5, 0.5), "runs past the edge"),
        ],
    )
    def test_an_impossible_region_says_why(self, region, message):
        with pytest.raises(ValueError, match=message):
            crop_jpeg(jpeg(640, 480), region, CROP_QUALITY)


class TestFrameSize:
    def test_reads_the_dimensions_out_of_a_jpeg(self):
        assert frame_size(jpeg(1280, 720)) == (1280, 720)

    def test_returns_none_rather_than_raising_on_junk(self):
        """Used by camera_status, where a frame that cannot be parsed should cost
        one field rather than the whole status call."""
        assert frame_size(b"not a jpeg at all") is None


class TestValidationHappensBeforeTheCamera:
    """A region that cannot work must be refused without touching the camera.

    Every check is arithmetic on fractions, so none of it needs a frame. Doing it
    late would mean a doomed call still woke the stream and pushed back the
    control-restore timer before failing.
    """

    @pytest.mark.parametrize(
        "region",
        [
            [0, 0, 2, 2],          # width and height out of range
            [0.8, 0, 0.5, 0.5],    # runs off the right edge
            [0.5, 0.5],            # wrong number of coordinates
            [0, 0, 0, 0.5],        # zero width
            [-0.1, 0, 0.5, 0.5],   # negative origin
        ],
    )
    def test_rejected_without_a_frame(self, region):
        from cam2mcp_server import validate_region

        with pytest.raises(ValueError):
            validate_region(region)

    def test_a_good_region_comes_back_as_a_box(self):
        from cam2mcp_server import validate_region

        assert validate_region([0.5, 0.0, 0.25, 0.75]) == (0.5, 0.0, 0.25, 0.75)

    async def test_grab_frame_refuses_before_reaching_the_camera(self, monkeypatch):
        """The claim worth pinning is not that validate_region rejects -- it is
        that grab_frame consults it *first*. Validating after the grab would
        still raise the same error, having already woken the stream and pushed
        back the control-restore timer."""
        import cam2mcp_server

        grabs = []

        class NeverCalled:
            async def grab(self, max_age_s=None):
                grabs.append(max_age_s)
                raise AssertionError("the camera was touched for a doomed region")

        monkeypatch.setattr(cam2mcp_server, "source", NeverCalled)

        with pytest.raises(ValueError, match="runs past the edge"):
            await cam2mcp_server.grab_frame(region=[0.8, 0.0, 0.5, 0.5])

        assert grabs == []
