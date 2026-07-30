"""Regression tests for the stale-frame bug.

The assertions here are timing-independent where it matters: each test notes the
highest frame the fake camera had captured *before* the grab, and then requires
the frame it got back to be a later one. Any frame from the pre-existing queue
fails that, which is exactly the bug.
"""

from __future__ import annotations

import asyncio
import time

import httpx2
import pytest

from cam2ip_mcp_server import FrameSource, FrameUnavailable
from conftest import make_config
from fake_cam2ip import read_frame_meta, running_fake_cam2ip


@pytest.fixture
async def source_factory():
    """Build FrameSources and guarantee they are shut down after the test."""
    created: list[FrameSource] = []

    def factory(base_url: str, **overrides) -> FrameSource:
        source = FrameSource(make_config(base_url, **overrides))
        created.append(source)
        return source

    yield factory
    for source in created:
        await source.aclose()


class TestTheBugItself:
    async def test_one_shot_snapshot_returns_a_stale_frame(self, fake_cam):
        """Baseline: what the old implementation did, and why it looked broken.

        One GET per request dequeues one buffer, so an idle pipeline hands back
        whatever it was sitting on.
        """
        await asyncio.sleep(0.5)  # nothing draining; the queue holds old frames
        stale_high_water = fake_cam.queue.captured

        async with httpx2.AsyncClient() as client:
            response = await client.get(f"{fake_cam.base_url}/jpeg")
        meta = read_frame_meta(response.content)

        assert meta["seq"] <= stale_high_water, "fake camera is not modelling the queue"
        assert time.time() - meta["captured_at"] > 0.4, "expected a pre-idle frame"

    async def test_consecutive_snapshots_walk_through_the_stale_queue(self, fake_cam):
        """And the frames after it are the *rest* of the old queue, in order."""
        await asyncio.sleep(0.5)
        stale_high_water = fake_cam.queue.captured

        async with httpx2.AsyncClient() as client:
            seqs = [
                read_frame_meta((await client.get(f"{fake_cam.base_url}/jpeg")).content)["seq"]
                for _ in range(3)
            ]

        assert seqs == sorted(seqs)
        assert all(seq <= stale_high_water for seq in seqs)


class TestTheFix:
    async def test_grab_skips_the_stale_queue(self, fake_cam, source_factory):
        """The headline case: first grab after an idle period is a live frame."""
        await asyncio.sleep(0.5)
        stale_high_water = await fake_cam.queue.wait_until_stalled()
        source = source_factory(fake_cam.base_url)

        frame = await source.grab()
        meta = read_frame_meta(frame.data)

        assert meta["seq"] > stale_high_water, (
            f"served frame {meta['seq']} from the stale queue "
            f"(everything <= {stale_high_water} predates the call)"
        )
        assert time.time() - meta["captured_at"] < 1.0
        assert frame.content_type == "image/jpeg"
        assert frame.data.startswith(b"\xff\xd8")

    async def test_repeated_grabs_keep_returning_newer_frames(self, fake_cam, source_factory):
        source = source_factory(fake_cam.base_url, frame_max_age_s=0.2)

        seqs = []
        for _ in range(4):
            frame = await source.grab()
            seqs.append(read_frame_meta(frame.data)["seq"])
            await asyncio.sleep(0.3)

        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs

    async def test_grab_on_a_warm_stream_returns_a_frame_from_just_now(
        self, fake_cam, source_factory
    ):
        """While the stream is up, the newest frame is always a picture of now.

        The stalled-queue watermark cannot be used here: the pump is draining
        continuously, so the driver keeps capturing and its counter stays a few
        in-flight frames ahead of whatever has been delivered. The frame's own
        capture timestamp is the meaningful check.
        """
        source = source_factory(fake_cam.base_url)
        first = read_frame_meta((await source.grab()).data)

        await asyncio.sleep(1.0)
        second = read_frame_meta((await source.grab()).data)

        assert second["seq"] > first["seq"]
        assert time.time() - second["captured_at"] < 0.5

    async def test_a_recent_frame_is_served_without_waking_the_camera_again(
        self, fake_cam, source_factory
    ):
        """A frame still inside max_age is answered from memory.

        stream_idle_s=0 drops the subscription straight after the first grab, so
        a needless second camera wake-up would show up as another connection.
        """
        source = source_factory(fake_cam.base_url, frame_max_age_s=5.0, stream_idle_s=0.0)
        first = await source.grab()
        await asyncio.sleep(0.1)
        second = await source.grab()

        assert fake_cam.mjpeg_connections == 1
        first_meta, second_meta = read_frame_meta(first.data), read_frame_meta(second.data)
        assert second_meta["seq"] >= first_meta["seq"]

    async def test_stream_is_reused_between_close_together_grabs(self, fake_cam, source_factory):
        """A warm stream should be reused, not reconnected per call."""
        source = source_factory(fake_cam.base_url, stream_idle_s=30.0)
        for _ in range(3):
            await source.grab()

        assert fake_cam.mjpeg_connections == 1
        assert fake_cam.jpeg_requests == 0, "the snapshot endpoint should not be used"

    async def test_idle_stream_is_dropped_then_restarted_fresh(self, fake_cam, source_factory):
        """Going idle releases the camera; the next grab still gets a live frame."""
        # max_age below the gap we wait, so the second grab cannot be answered
        # from the frame the first one left in memory.
        source = source_factory(fake_cam.base_url, stream_idle_s=0.0, frame_max_age_s=0.2)
        await source.grab()

        async def stream_stopped() -> bool:
            for _ in range(200):
                if not source.status()["stream_running"]:
                    return True
                await asyncio.sleep(0.01)
            return False

        assert await stream_stopped(), "stream should shut down when idle"

        # Nothing is draining now, so the queue refills and goes stale.
        await asyncio.sleep(0.5)
        stale_high_water = await fake_cam.queue.wait_until_stalled()

        meta = read_frame_meta((await source.grab()).data)
        assert meta["seq"] > stale_high_water
        assert fake_cam.mjpeg_connections == 2


class TestWarmup:
    async def test_warmup_drops_at_least_the_whole_buffer_queue(self, source_factory):
        """A queue deeper than warmup_frames is still flushed, thanks to warmup_s."""
        async with running_fake_cam2ip(depth=12, fps=60.0) as fake:
            await asyncio.sleep(0.7)  # fill all 12 buffers, then let them age
            stale_high_water = fake.queue.captured
            assert stale_high_water >= 12

            source = source_factory(fake.base_url, warmup_frames=2, warmup_s=0.5)
            meta = read_frame_meta((await source.grab()).data)

            assert meta["seq"] > stale_high_water

    async def test_disabling_warmup_exposes_the_stale_queue(self, fake_cam, source_factory):
        """Documents why warm-up is load-bearing rather than belt-and-braces.

        Arrival time cannot distinguish a queued frame from a live one, so with
        warm-up off the first frame served after an idle period is stale again.
        """
        await asyncio.sleep(0.5)
        stale_high_water = fake_cam.queue.captured
        source = source_factory(fake_cam.base_url, warmup_frames=0, warmup_s=0.0)

        meta = read_frame_meta((await source.grab()).data)
        assert meta["seq"] <= stale_high_water


class TestFailureModes:
    async def test_reports_a_useful_error_when_cam2ip_is_down(self, source_factory):
        source = source_factory("http://127.0.0.1:1", grab_timeout_s=1.0)

        with pytest.raises(FrameUnavailable) as excinfo:
            await source.grab()

        message = str(excinfo.value)
        assert "no frame newer than" in message
        assert "last error" in message  # carries the connection failure

    async def test_reports_a_useful_error_when_mjpeg_is_unavailable(self, source_factory):
        async with running_fake_cam2ip(mjpeg_status=404) as fake:
            source = source_factory(fake.base_url, grab_timeout_s=1.0)

            with pytest.raises(FrameUnavailable) as excinfo:
                await source.grab()

            assert "404" in str(excinfo.value)

    async def test_gives_up_immediately_on_an_error_a_retry_cannot_fix(self, source_factory):
        """A 404 will still be a 404 at the deadline, so do not make callers wait."""
        async with running_fake_cam2ip(mjpeg_status=404) as fake:
            source = source_factory(fake.base_url, grab_timeout_s=30.0)

            started = time.monotonic()
            with pytest.raises(FrameUnavailable, match="404"):
                await source.grab()
            elapsed = time.monotonic() - started

        assert elapsed < 5.0, f"waited {elapsed:.1f}s for an answer that could not change"
        assert source.status()["last_error_is_permanent"] is True

    async def test_keeps_retrying_an_error_a_retry_might_fix(self, source_factory):
        """A refused connection may be cam2ip still starting, so ride it out."""
        source = source_factory("http://127.0.0.1:1", grab_timeout_s=1.5)

        started = time.monotonic()
        with pytest.raises(FrameUnavailable):
            await source.grab()
        elapsed = time.monotonic() - started

        assert elapsed >= 1.4, f"gave up after {elapsed:.2f}s instead of waiting out the deadline"
        assert source.status()["last_error_is_permanent"] is False

    async def test_a_fixed_endpoint_is_retried_rather_than_written_off(self, fake_cam, source_factory):
        """A permanent verdict must not outlive the pump that reached it.

        While that pump is alive the verdict is legitimately current -- its retry
        loop keeps re-testing the endpoint -- so this waits for it to go idle
        first. That is the boundary being asserted: a *new* pump starts clean.
        """
        source = source_factory(fake_cam.base_url, grab_timeout_s=5.0, stream_idle_s=0.0)

        fake_cam.mjpeg_status = 404
        with pytest.raises(FrameUnavailable, match="404"):
            await source.grab()

        for _ in range(200):
            if not source.status()["stream_running"]:
                break
            await asyncio.sleep(0.05)
        assert source.status()["stream_running"] is False

        # Whatever was wrong has been put right; the next grab must try again.
        fake_cam.mjpeg_status = 200
        frame = await source.grab()
        assert frame.data.startswith(b"\xff\xd8")
        assert source.status()["last_error_is_permanent"] is False

    async def test_a_still_failing_endpoint_is_re_tested_by_the_retry_loop(self, fake_cam, source_factory):
        """And while the pump lives, a fix is picked up within one backoff."""
        source = source_factory(fake_cam.base_url, grab_timeout_s=5.0, stream_idle_s=30.0)

        fake_cam.mjpeg_status = 404
        with pytest.raises(FrameUnavailable, match="404"):
            await source.grab()
        assert source.status()["stream_running"] is True

        # No restart, no new grab: the running pump's own retry should notice.
        fake_cam.mjpeg_status = 200
        for _ in range(200):
            if not source.status()["last_error_is_permanent"]:
                break
            await asyncio.sleep(0.05)

        assert source.status()["last_error_is_permanent"] is False
        assert (await source.grab()).data.startswith(b"\xff\xd8")


class TestIdleWhileFailing:
    async def test_stops_reconnecting_once_nothing_is_waiting(self, source_factory):
        """The retry loop has to honour the idle timeout like the read loop does.

        Otherwise it is the one path that never checks, and a stream that cannot
        be established -- a wrong base URL, say -- reconnects every couple of
        seconds for the life of the process, long after the request that started
        it gave up.
        """
        source = source_factory(
            "http://127.0.0.1:1", grab_timeout_s=0.5, stream_idle_s=0.2
        )

        with pytest.raises(FrameUnavailable):
            await source.grab()

        assert source.status()["stream_running"] is True, "should still be retrying"

        for _ in range(200):
            if not source.status()["stream_running"]:
                break
            await asyncio.sleep(0.05)

        assert source.status()["stream_running"] is False, (
            "pump kept reconnecting with nobody waiting for a frame"
        )

    async def test_keeps_reconnecting_while_a_grab_is_waiting(self, source_factory):
        """Idle means nobody waiting -- an in-flight grab must hold it open."""
        source = source_factory(
            "http://127.0.0.1:1", grab_timeout_s=1.5, stream_idle_s=0.0
        )

        async def watch_while_waiting() -> list[bool]:
            seen = []
            for _ in range(6):
                await asyncio.sleep(0.2)
                seen.append(bool(source.status()["stream_running"]))
            return seen

        grab = asyncio.ensure_future(source.grab())
        running = await watch_while_waiting()
        with pytest.raises(FrameUnavailable):
            await grab

        assert any(running), "pump gave up while a grab was still waiting"

    async def test_recovers_after_the_stream_drops(self, fake_cam, source_factory):
        """A dropped connection must reconnect, not wedge."""
        source = source_factory(fake_cam.base_url, frame_max_age_s=0.2)
        await source.grab()

        # Kill the pump the way a network blip would.
        source._task.cancel()  # noqa: SLF001 - simulating an abrupt stream loss
        await asyncio.sleep(0.5)

        stale_high_water = await fake_cam.queue.wait_until_stalled()
        meta = read_frame_meta((await source.grab()).data)
        assert meta["seq"] > stale_high_water

    async def test_concurrent_grabs_all_get_a_fresh_frame(self, fake_cam, source_factory):
        await asyncio.sleep(0.5)
        stale_high_water = fake_cam.queue.captured
        source = source_factory(fake_cam.base_url)

        frames = await asyncio.gather(*(source.grab() for _ in range(5)))

        assert fake_cam.mjpeg_connections == 1
        for frame in frames:
            assert read_frame_meta(frame.data)["seq"] > stale_high_water

    async def test_grab_rejects_a_negative_max_age(self, fake_cam, source_factory):
        source = source_factory(fake_cam.base_url)
        # max_age of 0 means "must have arrived after I asked", which is valid.
        frame = await source.grab(max_age_s=0.0)
        assert frame.data


class TestStatus:
    async def test_status_reports_the_stream(self, fake_cam, source_factory):
        source = source_factory(fake_cam.base_url)
        assert source.status()["stream_connected"] is False

        await source.grab()
        status = source.status()

        assert status["stream_connected"] is True
        assert status["frames_received"] > status["frames_published"] > 0
        assert status["last_frame_age_s"] < 1.0
        assert status["last_error"] is None
        assert status["cam2ip_url"] == fake_cam.base_url
