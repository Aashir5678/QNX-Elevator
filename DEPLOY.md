# Deploying to qnxpi12

Getting code onto the board. `qnxpi12` has **LAN connectivity only, no internet route** —
nothing here fetches anything from the network, and no package installation is involved.

> **Status of this document.** The ssh/scp path below is based on community QNX 8.0.x
> documentation and is **not officially confirmed for this board**. Two specifics are called
> out as *confirm on first connect* and should be checked before this file is treated as
> settled. The SD-card fallback needs no networking at all and is the guaranteed path if ssh
> turns out not to be enabled on this image.

---

## Primary path: scp/ssh over the LAN

The QNX Quick Start Target Image is documented as running `sshd` by default, so `ssh` and
`scp` should both work once the board has a LAN IP. No internet route is needed — the host and
the board only have to be on the same network.

### 1. Find the board's IP

On the board (via serial console or attached keyboard/monitor):

```
ifconfig
```

Take the address on the active LAN interface. Then from the host, confirm reachability:

```
ping <board-ip>
```

### 2. Connect

```
ssh <user>@<board-ip>
```

#### ⚠ Confirm on first connect: root login may be disabled remotely

Remote `root` login over ssh is commonly disabled by default. If `ssh root@<ip>` is refused,
log in as the **default non-root user** and escalate once connected:

```
ssh qnxuser@<board-ip>      # default non-root user, name to be confirmed
su root
```

This matches the pattern already used elsewhere in this project's docs for GPIO and I2C
access — the elevator processes need root (or equivalent privilege) to touch GPIO, so expect
to `su root` before running `floor_input.py` and `motor_control.py` regardless of how the
files got there.

**Record the answer here once checked:** *(is remote root login permitted? what is the
default non-root username on this image?)*

#### ⚠ Confirm on first connect: scp protocol version

macOS's `scp` now uses the **SFTP** protocol by default rather than the legacy SCP protocol.
If the board's implementation does not support SFTP, transfers will fail — force the legacy
protocol with `-O`:

```
scp -O <files> <user>@<board-ip>:<dest>
```

Try plain `scp` first; add `-O` only if it fails. The `make deploy` target below leaves this
configurable for exactly this reason and does **not** assume either answer.

**Record the answer here once checked:** *(does plain scp work, or is `-O` required?)*

### 3. Deploy with make

```
make deploy HOST=qnxuser@192.168.1.50
```

Options:

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | *(required)* | `user@ip` of the board |
| `DEST` | `elevator` | destination directory, relative to the remote home |
| `SCP_FLAGS` | *(empty)* | set to `-O` if the SFTP-protocol issue above bites |

```
make deploy HOST=qnxuser@192.168.1.50 SCP_FLAGS=-O DEST=/tmp/elevator-src
```

Running `make deploy` with no `HOST` prints usage and exits without touching anything.

### What gets deployed

**Shipped** — the runtime files the board actually needs:

```
src/core.py  src/ipc.py  src/dispatcher.py  src/floor_input.py  src/motor_control.py
vision/blob.c  vision/blob.h  vision/vision_service.c
Makefile
```

**Not shipped** — host-only development files: `tests/`, `sim/`, `vision/test_blob.c`. These
are for tuning and validating off-device (see TESTING.md) and have no role on the board.

### vision_service must be built on the board

`vision_service` links against `libcapture` and must be an aarch64 QNX binary. It **cannot be
built on a non-QNX host** without the SDP cross-toolchain, so `make deploy` ships the C
sources and you build them on the board:

```
ssh <user>@<board-ip>
cd elevator
make vision            # -> build/vision_service
```

If you later set up SDP cross-compilation on the host and produce a real
`build/vision_service`, `make deploy` will detect and ship it automatically. Note that
`make vision-stub` produces `build/vision_stub` — a *host-only* synthetic-camera binary that
is never deployed and is not a substitute.

The Python files need no build step. Confirm the board has `python3` and that
`import rpi_gpio` works before expecting `floor_input.py` or `motor_control.py` to run.

---

## Fallback path: SD card or USB, no networking

Use this if ssh turns out not to be enabled on this image, or the board has no usable LAN
address. Nothing here depends on the network.

### Via the SD card

1. Power the board down and remove the SD card.
2. Mount it on the host.
3. Copy the same file set listed above onto the card, preserving the `src/` and `vision/`
   layout. The QNX filesystem partition may not be writable from macOS — if it is not, copy
   into any partition the host *can* write (commonly the FAT boot partition) and move the
   files into place from the board's own shell after boot.
4. Unmount cleanly, reinsert, and boot.
5. On the board, move the files to a working directory if needed and continue with
   `make vision` as above.

### Via USB mass storage

1. Copy the file set to a USB stick formatted so both machines can read it (FAT32 is the
   safest common denominator).
2. Insert it into the board and mount it. QNX typically exposes removable storage under
   `/fs/` — check with `ls /fs` after inserting, since the exact mount point depends on the
   image.
3. Copy the files off the stick into a working directory on the board.

**Which partitions are writable from macOS, and where USB media mounts on this image, are
both unverified.** Check on first use and record the answers here.

---

## After deploying

Run the system per **[USAGE.md](USAGE.md)** — startup order matters (`dispatcher` first, it
creates the FIFOs). Before the servo is energised, work through the calibration procedure in
**[TESTING.md](TESTING.md)** Layer 3: `FLOOR_ANGLES` are `None` placeholders and
`motor_control.py` refuses to start until they are measured.

## Re-deploying after a change

`make deploy` overwrites in place; it does not clean the destination first. Removing a source
file on the host will **not** remove the stale copy on the board. If a file is deleted or
renamed, delete it on the board manually or wipe `DEST` and redeploy.
