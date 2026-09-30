"""Sliding-window mean of the setpoints that arrived recently.

Pure logic with no network and no clock of its own: the caller passes the time,
so the tests can drive any timeline. The window is half-open, (now - window, now]:
a sample exactly one window old is out, one that arrived at `now` is in. With a
tick every window, that puts each sample in exactly one tick's average.
"""

from collections import deque


class WindowAverager:
    def __init__(self, window_s):
        self._window_s = window_s
        self._samples = deque()  # (arrival, watts), oldest first

    def add(self, now, watts):
        """Record a sample that arrived at `now`. Arrival times must not go backwards."""
        self._samples.append((now, watts))

    def average(self, now):
        """Return (count, mean) of the samples younger than the window.

        The mean is None when the count is 0. Samples that have aged out are
        dropped, so memory stays bounded.
        """
        while self._samples and now - self._samples[0][0] >= self._window_s:
            self._samples.popleft()
        count = len(self._samples)
        if count == 0:
            return 0, None
        return count, sum(watts for _, watts in self._samples) / count

    def __len__(self):
        return len(self._samples)
