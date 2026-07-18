#!/usr/bin/env python3
"""motor_control -- drives the servo that moves the car.

Reads  FIFO_TARGET  {"target": N} from dispatcher
Writes FIFO_ARRIVED {"arrived": N} once the servo has settled
Writes FIFO_CARPOS  {"car_floor": N, "moving": bool} continuously, so
       vision_service knows which ROI band to suppress.
"""

import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import ipc

# =========================================================================
# PLACEHOLDER CALIBRATION -- these are NOT real angles.
#
# Every value below must be measured on the physical rig before the servo is
# allowed to drive the car. Running with these as-is may slam the car into an
# end stop. Procedure: detach the drive linkage, jog the servo one degree at a
# time, and record the angle at which the car sits level with each floor.
# =========================================================================
FLOOR_ANGLES = {
    1: None,  # TODO: measure -- angle with car level at floor 1
    2: None,  # TODO: measure -- angle with car level at floor 2
    3: None,  # TODO: measure -- angle with car level at floor 3
}

# Also placeholders, to be measured:
SERVO_PIN = 18  # BCM. Must be a hardware-PWM capable pin.
PWM_FREQUENCY_HZ = 50  # standard hobby servo frame rate; verify for your servo
SETTLE_TIME_PER_FLOOR = None  # TODO: time a one-floor move, add margin
SETTLE_TIME_MIN = None  # TODO: minimum settle even for a zero-distance move

CARPOS_INTERVAL = 0.25
POLL_INTERVAL = 0.05

# VERIFY ON DEVICE / AGAINST QNX DOCS -- do not trust the mapping below
# without checking it:
#
# The angle -> duty-cycle conversion depends on how QNX's rpi_gpio PWM
# expresses duty cycle in MS mode. RPi.GPIO-style ChangeDutyCycle() takes a
# PERCENT (0-100), while pigpio-style APIs take a pulse WIDTH in microseconds.
# These are not interchangeable and getting it wrong will drive the servo to a
# hard stop. Confirm which one QNX's module implements, and confirm the exact
# call used to select MS mode, before energising the servo.
#
# The conversion below assumes PERCENT. If QNX's module wants microseconds,
# use pulse_us directly and delete the percent conversion.
SERVO_MIN_PULSE_US = 1000  # typical 0deg; verify for your specific servo
SERVO_MAX_PULSE_US = 2000  # typical 180deg; verify for your specific servo
SERVO_MAX_ANGLE = 180.0


def angle_to_duty_percent(angle, freq_hz=PWM_FREQUENCY_HZ):
    """Convert a servo angle to a duty-cycle percentage.

    See the VERIFY block above -- this assumes a percent-based API.
    """
    span = SERVO_MAX_PULSE_US - SERVO_MIN_PULSE_US
    pulse_us = SERVO_MIN_PULSE_US + (angle / SERVO_MAX_ANGLE) * span
    frame_us = 1_000_000.0 / freq_hz
    return pulse_us / frame_us * 100.0


def check_calibrated():
    """Refuse to drive the servo with placeholder values still in place."""
    missing = [f for f, a in FLOOR_ANGLES.items() if a is None]
    if missing or SETTLE_TIME_PER_FLOOR is None or SETTLE_TIME_MIN is None:
        raise SystemExit(
            "motor_control: refusing to start -- calibration values are still "
            f"placeholders (floors missing angles: {missing}). Measure them on "
            "the rig and edit the constants at the top of this file."
        )


class CarModel:
    """Position/timing bookkeeping. Hardware-free, so it is unit testable."""

    def __init__(self, start_floor=1):
        self.floor = start_floor
        self.target = None
        self.arrive_at = None

    def start_move(self, target, now):
        distance = abs(target - self.floor)
        self.target = target
        self.arrive_at = now + max(
            SETTLE_TIME_MIN, distance * SETTLE_TIME_PER_FLOOR
        )

    @property
    def moving(self):
        return self.target is not None

    def poll(self, now):
        """Returns the floor just arrived at, or None."""
        if self.target is None or now < self.arrive_at:
            return None
        self.floor = self.target
        self.target = None
        self.arrive_at = None
        return self.floor

    def reported_floor(self):
        """Which ROI band vision should suppress.

        While moving we report the DESTINATION, not the origin. The car leaves
        the origin band almost immediately, and suppressing the destination
        early is the safe error: a briefly-suppressed empty band just delays a
        count, whereas an unsuppressed car gets miscounted as a head.
        """
        return self.target if self.target is not None else self.floor


def main():
    import rpi_gpio as GPIO

    check_calibrated()
    ipc.ensure_fifos()
    target_in = ipc.FifoReader(ipc.FIFO_TARGET)
    arrived_out = ipc.FifoWriter(ipc.FIFO_ARRIVED)
    carpos_out = ipc.FifoWriter(ipc.FIFO_CARPOS)

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(SERVO_PIN, GPIO.OUT)
    pwm = GPIO.PWM(SERVO_PIN, PWM_FREQUENCY_HZ)
    # VERIFY: the exact call to select MS mode. QNX documents MS mode for
    # servos, but the setter name/signature must be confirmed in the docs.
    # pwm.set_mode(GPIO.PWM_MODE_MS)   # <-- confirm before uncommenting
    pwm.start(angle_to_duty_percent(FLOOR_ANGLES[1]))

    car = CarModel(start_floor=1)
    last_carpos = 0.0
    print("[motor_control] up", flush=True)
    try:
        while True:
            now = time.time()

            for msg in target_in.poll():
                target = msg.get("target")
                if target is None or int(target) not in FLOOR_ANGLES:
                    continue
                target = int(target)
                if car.moving:
                    # Dispatcher only sends a target when it believes the car
                    # is idle, so this means the two have desynchronized.
                    # Ignore it rather than reversing mid-travel.
                    print(
                        f"[motor_control] ignoring target {target}, still moving",
                        flush=True,
                    )
                    continue
                car.start_move(target, now)
                pwm.ChangeDutyCycle(angle_to_duty_percent(FLOOR_ANGLES[target]))
                print(f"[motor_control] moving to floor {target}", flush=True)

            arrived = car.poll(now)
            if arrived is not None:
                arrived_out.send({"arrived": arrived})
                print(f"[motor_control] arrived at floor {arrived}", flush=True)

            if now - last_carpos >= CARPOS_INTERVAL:
                carpos_out.send({"car_floor": car.reported_floor(), "moving": car.moving})
                last_carpos = now

            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        pwm.stop()
        for c in (target_in, arrived_out, carpos_out):
            c.close()
        GPIO.cleanup()


if __name__ == "__main__":
    main()
