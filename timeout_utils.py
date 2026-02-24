"""Shared timeout utilities for Shraga services.

Two primitives:
- call_with_timeout: run any blocking function in a thread with a hard deadline
- PipeReader: non-blocking subprocess pipe reader using background thread + queue
"""
import threading
import time
from queue import Queue, Empty


def call_with_timeout(fn, timeout_sec=30, description="operation"):
    """Run fn() in a daemon thread with a hard timeout.

    Returns the result of fn().
    Raises TimeoutError if fn() doesn't complete within timeout_sec.
    Re-raises any exception fn() throws.
    """
    result = [None]
    error = [None]

    def _target():
        try:
            result[0] = fn()
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if t.is_alive():
        raise TimeoutError(f"{description} timed out after {timeout_sec}s")

    if error[0] is not None:
        raise error[0]

    return result[0]


class PipeReader:
    """Non-blocking reader for subprocess pipes using a background thread + queue.

    Starts a daemon thread that reads lines from the pipe into a Queue.
    readline() and read_all() pull from the queue with timeouts -- never block forever.

    Usage:
        reader = PipeReader(process.stdout)
        line = reader.readline(timeout=60)      # '' on timeout or EOF
        remaining = reader.read_all(timeout=10)  # drain with deadline
    """

    def __init__(self, pipe):
        self._queue = Queue()
        self._eof = False
        self._thread = threading.Thread(
            target=self._reader_loop, args=(pipe,), daemon=True
        )
        self._thread.start()

    def _reader_loop(self, pipe):
        """Read lines from pipe into the queue until EOF."""
        try:
            while True:
                line = pipe.readline()
                if not line:  # EOF
                    break
                self._queue.put(line)
        except Exception:
            pass
        finally:
            self._queue.put(None)  # sentinel for EOF

    def readline(self, timeout=60):
        """Get one line from the pipe. Returns '' on timeout or EOF."""
        if self._eof:
            return ''
        try:
            line = self._queue.get(timeout=timeout)
            if line is None:
                self._eof = True
                return ''
            return line
        except Empty:
            return ''  # timeout

    def read_all(self, timeout=10):
        """Drain all remaining content from the pipe within timeout.
        Returns whatever was collected (may be partial on timeout)."""
        lines = []
        deadline = time.time() + timeout
        while True:
            if self._eof:
                break
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                line = self._queue.get(timeout=min(remaining, 0.5))
                if line is None:
                    self._eof = True
                    break
                lines.append(line)
            except Empty:
                continue  # check deadline
        return ''.join(lines)
