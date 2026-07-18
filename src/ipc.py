"""Named-FIFO line protocol shared by all four processes.

One JSON object per line, newline-terminated. Chosen over POSIX message queues
because Python mqueue bindings are not confirmed available on this SDP 8.0.0
image; chosen over sockets because the topology is fixed single-writer /
single-reader per channel.

Writes are done as one os.write() of a single line. On QNX (as on POSIX
generally) a write smaller than PIPE_BUF to a FIFO is atomic, which keeps
lines from interleaving. Keep messages small -- they are all well under 1 KiB.
"""

import errno
import json
import os

FIFO_DIR = os.environ.get("ELEVATOR_FIFO_DIR", "/tmp/elevator")

# vision_service -> dispatcher : {"heads": {"1": 0, "2": 3, "3": 1}}
FIFO_HEADS = os.path.join(FIFO_DIR, "heads")
# floor_input -> dispatcher : {"calls": {"1": {"active": true, "since": 12345.6}}}
FIFO_CALLS = os.path.join(FIFO_DIR, "calls")
# dispatcher -> floor_input : {"served": 2}
FIFO_SERVED = os.path.join(FIFO_DIR, "served")
# dispatcher -> motor_control : {"target": 3}
FIFO_TARGET = os.path.join(FIFO_DIR, "target")
# motor_control -> dispatcher : {"arrived": 3}
FIFO_ARRIVED = os.path.join(FIFO_DIR, "arrived")
# motor_control -> vision_service : {"car_floor": 2, "moving": false}
FIFO_CARPOS = os.path.join(FIFO_DIR, "carpos")

ALL_FIFOS = (
    FIFO_HEADS,
    FIFO_CALLS,
    FIFO_SERVED,
    FIFO_TARGET,
    FIFO_ARRIVED,
    FIFO_CARPOS,
)


def ensure_fifos(paths=ALL_FIFOS):
    """Create any missing FIFOs. Safe to call from every process at startup."""
    os.makedirs(FIFO_DIR, exist_ok=True)
    for path in paths:
        try:
            os.mkfifo(path, 0o660)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise


class FifoWriter:
    """Non-blocking-open writer that tolerates the reader not being up yet.

    Opening a FIFO for write with O_NONBLOCK fails with ENXIO while no reader
    has it open. Rather than blocking the whole process at startup, we retry
    the open on each send and drop the message if nobody is listening.
    """

    def __init__(self, path):
        self.path = path
        self._fd = None

    def _open(self):
        if self._fd is not None:
            return True
        try:
            self._fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
            return True
        except OSError as exc:
            if exc.errno in (errno.ENXIO, errno.ENOENT):
                return False
            raise

    def send(self, obj) -> bool:
        """Serialize and write one line. Returns False if it was dropped."""
        if not self._open():
            return False
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        try:
            os.write(self._fd, line)
            return True
        except OSError as exc:
            # Reader went away (EPIPE) or its buffer is full (EAGAIN). Both are
            # recoverable: close and let the next send re-open. Dropping a
            # sample is fine -- every channel here republishes full state, not
            # deltas, so the next message resynchronizes the reader.
            if exc.errno in (errno.EPIPE, errno.EAGAIN):
                self.close()
                return False
            raise

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class FifoReader:
    """Line-buffered reader that never blocks and never loses partial lines."""

    def __init__(self, path):
        self.path = path
        # O_RDONLY|O_NONBLOCK on a FIFO succeeds immediately even with no
        # writer, and subsequent reads return EAGAIN rather than EOF.
        self._fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self._buf = b""

    def fileno(self):
        return self._fd

    def poll(self):
        """Return a list of complete messages available right now."""
        while True:
            try:
                chunk = os.read(self._fd, 4096)
            except OSError as exc:
                if exc.errno == errno.EAGAIN:
                    break
                raise
            if not chunk:
                break
            self._buf += chunk

        msgs = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except ValueError:
                # Truncated or corrupt line -- skip it, the next full-state
                # message will resynchronize us.
                continue
        return msgs

    def close(self):
        os.close(self._fd)
        self._fd = None
