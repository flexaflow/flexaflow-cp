"""TOTP tests, including the RFC 6238 test vectors (SHA1, 8 digits; we check the 6-digit tail).
    python -m unittest tests/test_totp.py -v
"""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import totp

# RFC 6238 Appendix B secret "12345678901234567890", base32:
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

class TotpTests(unittest.TestCase):
    def test_rfc6238_vectors_tail(self):
        # 8-digit RFC values -> their last 6 digits are what authenticator apps show
        vectors = {59: "287082", 1111111109: "081804", 1234567890: "005924", 2000000000: "279037"}
        for t, expected in vectors.items():
            counter = t // 30
            self.assertEqual(totp.hotp(RFC_SECRET, counter), expected, t)

    def test_verify_accepts_current_and_neighbour_steps(self):
        now = 1111111109
        code = totp.hotp(RFC_SECRET, totp.counter_at(now))
        self.assertEqual(totp.verify(RFC_SECRET, code, now=now), totp.counter_at(now))
        self.assertIsNotNone(totp.verify(RFC_SECRET, totp.hotp(RFC_SECRET, totp.counter_at(now) - 1), now=now))

    def test_rejects_old_and_wrong_codes(self):
        now = 1111111109
        old = totp.hotp(RFC_SECRET, totp.counter_at(now) - 5)
        self.assertIsNone(totp.verify(RFC_SECRET, old, now=now))
        self.assertIsNone(totp.verify(RFC_SECRET, "000000x", now=now))
        self.assertIsNone(totp.verify(RFC_SECRET, "12345", now=now))

    def test_replay_is_blocked(self):
        now = 1111111109
        code = totp.hotp(RFC_SECRET, totp.counter_at(now))
        used = totp.verify(RFC_SECRET, code, now=now)
        self.assertIsNone(totp.verify(RFC_SECRET, code, last_counter=used, now=now))

    def test_secret_and_uri(self):
        s = totp.new_secret()
        self.assertGreaterEqual(len(s), 32)
        uri = totp.provisioning_uri(s, "admin", "Contest Judge")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn(f"secret={s}", uri)

if __name__ == "__main__":
    unittest.main()
