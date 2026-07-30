"""Shared fixtures. Adds the repo root to sys.path so the server module imports."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cam2ip_mcp_server import Config  # noqa: E402
from fake_cam2ip import running_fake_cam2ip  # noqa: E402


def make_config(base_url: str, **overrides) -> Config:
    """A Config for tests: same defaults as production, tuned for speed."""
    settings = dict(
        base_url=base_url,
        http_timeout_s=2.0,
        mode="stdio",
        http_host="127.0.0.1",
        http_port=0,
        frame_max_age_s=1.0,
        grab_timeout_s=5.0,
        warmup_frames=5,
        warmup_s=0.25,
        stream_idle_s=30.0,
        allowed_hosts=None,
        allowed_origins=None,
    )
    settings.update(overrides)
    return Config(**settings)


@pytest.fixture
async def fake_cam():
    """A fake cam2ip whose buffer queue has already gone stale.

    The queue fills (4 buffers at 30fps) and the driver then stalls, so by the
    time a test runs, every buffered frame predates it -- the exact state that
    made the real server hand back old pictures.
    """
    async with running_fake_cam2ip(depth=4, fps=30.0) as fake:
        await fake.queue.wait_until_stalled()
        yield fake
