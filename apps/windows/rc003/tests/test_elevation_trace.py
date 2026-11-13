import unittest

from ovb_rc003 import elevation_trace


class ElevationTraceTests(unittest.TestCase):
    def test_uac_cancelled_maps_to_cancelled_outcome(self):
        with self.assertLogs("ovb_rc003", level="INFO") as captured:
            elevation_trace.log_uac_outcome(
                "test_kind",
                "launch_failed",
                winerror=elevation_trace._WINERROR_UAC_CANCELLED,
            )
        joined = "\n".join(captured.output)
        self.assertIn("result=cancelled", joined)
        self.assertIn("kind=test_kind", joined)


if __name__ == "__main__":
    unittest.main()
