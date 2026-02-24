"""Tests for timeout_utils.py -- call_with_timeout and PipeReader."""
import io
import threading
import time

import pytest

from timeout_utils import call_with_timeout, PipeReader


# ===========================================================================
# call_with_timeout
# ===========================================================================

class TestCallWithTimeout:

    def test_success_returns_result(self):
        result = call_with_timeout(lambda: 42, timeout_sec=5)
        assert result == 42

    def test_timeout_raises(self):
        with pytest.raises(TimeoutError, match="timed out after 1s"):
            call_with_timeout(lambda: time.sleep(10), timeout_sec=1, description="slow op")

    def test_exception_reraised(self):
        def boom():
            raise ValueError("test error")
        with pytest.raises(ValueError, match="test error"):
            call_with_timeout(boom, timeout_sec=5)

    def test_custom_description_in_timeout_message(self):
        with pytest.raises(TimeoutError, match="my_func timed out"):
            call_with_timeout(lambda: time.sleep(10), timeout_sec=1, description="my_func")

    def test_returns_none_when_fn_returns_none(self):
        result = call_with_timeout(lambda: None, timeout_sec=5)
        assert result is None


# ===========================================================================
# PipeReader
# ===========================================================================

class TestPipeReader:

    def _make_pipe_with_data(self, lines):
        """Create a pipe-like object that yields lines then EOF.

        Uses a real threading.Thread + os.pipe to simulate a subprocess pipe,
        since PipeReader's background thread calls readline() on the pipe.
        """
        import os
        read_fd, write_fd = os.pipe()
        read_file = os.fdopen(read_fd, 'r')
        write_file = os.fdopen(write_fd, 'w')

        def _write():
            for line in lines:
                write_file.write(line)
                write_file.flush()
            write_file.close()

        t = threading.Thread(target=_write, daemon=True)
        t.start()
        return read_file

    def test_readline_returns_data(self):
        pipe = self._make_pipe_with_data(["hello\n", "world\n"])
        reader = PipeReader(pipe)
        line1 = reader.readline(timeout=5)
        assert line1 == "hello\n"
        line2 = reader.readline(timeout=5)
        assert line2 == "world\n"

    def test_readline_returns_empty_on_eof(self):
        pipe = self._make_pipe_with_data(["one\n"])
        reader = PipeReader(pipe)
        reader.readline(timeout=5)  # consume "one\n"
        line = reader.readline(timeout=5)  # EOF
        assert line == ''

    def test_readline_returns_empty_on_timeout(self):
        """readline returns '' when no data arrives within timeout."""
        import os
        read_fd, write_fd = os.pipe()
        read_file = os.fdopen(read_fd, 'r')
        # Don't write anything, don't close -- pipe stays open
        reader = PipeReader(read_file)
        line = reader.readline(timeout=0.5)
        assert line == ''
        # Cleanup
        os.close(write_fd)

    def test_read_all_returns_all_content(self):
        pipe = self._make_pipe_with_data(["a\n", "b\n", "c\n"])
        reader = PipeReader(pipe)
        # Give the reader thread a moment to fill the queue
        time.sleep(0.2)
        result = reader.read_all(timeout=5)
        assert "a\n" in result
        assert "b\n" in result
        assert "c\n" in result

    def test_read_all_partial_on_timeout(self):
        """read_all returns partial data when timeout expires before EOF."""
        import os
        read_fd, write_fd = os.pipe()
        read_file = os.fdopen(read_fd, 'r')
        write_file = os.fdopen(write_fd, 'w')

        # Write one line then keep pipe open (no EOF)
        write_file.write("first\n")
        write_file.flush()

        reader = PipeReader(read_file)
        time.sleep(0.2)  # let the reader thread pick up the line
        result = reader.read_all(timeout=1)
        assert "first\n" in result
        # Cleanup
        write_file.close()

    def test_readline_after_eof_returns_empty(self):
        pipe = self._make_pipe_with_data([])
        reader = PipeReader(pipe)
        assert reader.readline(timeout=2) == ''
        assert reader.readline(timeout=0.5) == ''  # subsequent calls also return ''
