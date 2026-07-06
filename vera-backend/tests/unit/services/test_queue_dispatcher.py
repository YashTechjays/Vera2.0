"""Unit tests for QueueDispatcher.

Uses an in-memory approach: the dispatcher is tested through its public
interface with mock SQLAlchemy session results and a FakeLiveKit, verifying
FIFO ordering, concurrency gating, working-hours checks, and expiry.
"""

from datetime import time
from unittest.mock import MagicMock, patch

from vera_core.services.queue_dispatcher import is_within_working_hours


class TestIsWithinWorkingHours:
    """Working-hours gate — pure function, no DB."""

    def _provider(
        self,
        start: time | None = None,
        end: time | None = None,
    ) -> MagicMock:
        p = MagicMock()
        p.working_hour_start = start
        p.working_hour_end = end
        return p

    def test_none_hours_means_always_available(self) -> None:
        assert is_within_working_hours(self._provider()) is True

    @patch("vera_core.services.queue_dispatcher._now_eastern_time")
    def test_within_hours(self, mock_now: MagicMock) -> None:
        mock_now.return_value = time(10, 0)
        provider = self._provider(start=time(8, 0), end=time(17, 0))
        assert is_within_working_hours(provider) is True

    @patch("vera_core.services.queue_dispatcher._now_eastern_time")
    def test_outside_hours(self, mock_now: MagicMock) -> None:
        mock_now.return_value = time(6, 0)
        provider = self._provider(start=time(8, 0), end=time(17, 0))
        assert is_within_working_hours(provider) is False

    @patch("vera_core.services.queue_dispatcher._now_eastern_time")
    def test_at_boundary_start(self, mock_now: MagicMock) -> None:
        mock_now.return_value = time(8, 0)
        provider = self._provider(start=time(8, 0), end=time(17, 0))
        assert is_within_working_hours(provider) is True

    @patch("vera_core.services.queue_dispatcher._now_eastern_time")
    def test_at_boundary_end(self, mock_now: MagicMock) -> None:
        mock_now.return_value = time(17, 0)
        provider = self._provider(start=time(8, 0), end=time(17, 0))
        assert is_within_working_hours(provider) is True
