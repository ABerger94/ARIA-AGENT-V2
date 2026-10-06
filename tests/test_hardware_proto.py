"""Unit tests for the robot-body wire protocol (aria/tools/hardware_proto.py).

Pure stdlib, no hardware, no network. Encoders must match the firmware
(arduino/aria_body/aria_body.ino) byte-for-byte: P<pan>T<tilt>, W<l>,<r>,
S — LF-terminated, 115200 baud.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aria.tools import hardware_proto as hp


class EncoderTests(unittest.TestCase):
    def test_encode_servo_exact_bytes(self):
        self.assertEqual(hp.encode_servo(90, 45), b"P90T45\n")
        self.assertEqual(hp.encode_servo(0, 0), b"P0T0\n")
        self.assertEqual(hp.encode_servo(180, 90), b"P180T90\n")

    def test_encode_servo_clamps(self):
        # v1 send_servo_command rules: pan 0..180, tilt 0..90
        self.assertEqual(hp.encode_servo(999, -5), b"P180T0\n")
        self.assertEqual(hp.encode_servo(-10, 500), b"P0T90\n")

    def test_encode_drive_exact_bytes(self):
        self.assertEqual(hp.encode_drive(50, -50), b"W50,-50\n")
        self.assertEqual(hp.encode_drive(0, 0), b"W0,0\n")

    def test_encode_drive_clamps(self):
        # v1 send_drive_command rules: -100..100
        self.assertEqual(hp.encode_drive(200, -300), b"W100,-100\n")

    def test_encode_stop_exact_bytes(self):
        self.assertEqual(hp.encode_stop(), b"S\n")

    def test_baud_matches_firmware(self):
        self.assertEqual(hp.BAUD, 115200)

    def test_clamp_servo_non_numeric_defaults_home(self):
        self.assertEqual(hp.clamp_servo("x", None), (90, 45))

    def test_clamp_wheels_non_numeric_defaults_zero(self):
        self.assertEqual(hp.clamp_wheels("x", None), (0, 0))


class PortMatchTests(unittest.TestCase):
    def test_matches_body_ports(self):
        self.assertTrue(hp.port_matches("Arduino Uno"))
        self.assertTrue(hp.port_matches("CH340"))
        self.assertTrue(hp.port_matches("USB Serial Device"))

    def test_rejects_others(self):
        self.assertFalse(hp.port_matches("FTDI USB"))
        self.assertFalse(hp.port_matches(""))
        self.assertFalse(hp.port_matches(None))


class HardwareStateTests(unittest.TestCase):
    def test_initial_snapshot(self):
        st = hp.HardwareState()
        snap = st.snapshot()
        self.assertEqual(snap, {"connected": False, "pan": 90, "tilt": 45,
                               "wheels": {"left": 0, "right": 0},
                               "serial_available": False})

    def test_note_servo_drive_stop(self):
        st = hp.HardwareState()
        st.note_servo(10, 80)
        st.note_drive(60, -60)
        snap = st.snapshot()
        self.assertEqual((snap["pan"], snap["tilt"]), (10, 80))
        self.assertEqual(snap["wheels"], {"left": 60, "right": -60})
        st.note_stop()
        snap = st.snapshot()
        self.assertEqual(snap["wheels"], {"left": 0, "right": 0})
        # v1 body_stop re-centered the head (firmware does too on 'S')
        self.assertEqual((snap["pan"], snap["tilt"]), (90, 45))

    def test_state_clamps(self):
        st = hp.HardwareState()
        st.note_servo(999, 999)
        st.note_drive(999, -999)
        snap = st.snapshot()
        self.assertEqual((snap["pan"], snap["tilt"]), (180, 90))
        self.assertEqual(snap["wheels"], {"left": 100, "right": -100})


if __name__ == "__main__":
    unittest.main(verbosity=2)
