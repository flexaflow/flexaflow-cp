import os, sys, unittest, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import services as sv

START = dt.datetime(2026, 11, 22, 10, 0, tzinfo=dt.timezone.utc)

def at(minutes):
    return (START + dt.timedelta(minutes=minutes)).isoformat()

class DuplicateTests(unittest.TestCase):
    def test_whitespace_and_line_endings_are_the_same_code(self):
        a = "print(1)\nprint(2)\n"
        b = "print(1)   \r\n\r\nprint(2)"
        self.assertEqual(sv.code_hash(a), sv.code_hash(b))

    def test_real_change_is_different_code(self):
        self.assertNotEqual(sv.code_hash("print(1)"), sv.code_hash("print(2)"))

class ProgressTests(unittest.TestCase):
    def test_solved_after_wrong_attempts(self):
        rows = [
            {"problem_id": 1, "verdict": "Wrong Answer", "submitted_at": at(5)},
            {"problem_id": 1, "verdict": "Time Limit Exceeded", "submitted_at": at(7)},
            {"problem_id": 1, "verdict": "Accepted", "submitted_at": at(30)},
            {"problem_id": 1, "verdict": "Wrong Answer", "submitted_at": at(40)},  # after solve: ignored
        ]
        p = sv.progress_for(rows, START)[1]
        self.assertTrue(p["solved"])
        self.assertEqual(p["solved_min"], 30)
        self.assertEqual(p["wrong"], 2)
        self.assertEqual(p["attempts"], 3)

    def test_pending_and_judge_errors(self):
        rows = [
            {"problem_id": 2, "verdict": "Pending", "submitted_at": at(1)},
            {"problem_id": 2, "verdict": "Internal Error", "submitted_at": at(2)},
            {"problem_id": 2, "verdict": "Restricted Code", "submitted_at": at(3)},
        ]
        p = sv.progress_for(rows, START)[2]
        self.assertTrue(p["pending"])
        self.assertEqual(p["attempts"], 0)
        self.assertEqual(p["wrong"], 0)
        self.assertFalse(p["solved"])

    def test_problems_are_independent(self):
        rows = [
            {"problem_id": 1, "verdict": "Accepted", "submitted_at": at(3)},
            {"problem_id": 2, "verdict": "Wrong Answer", "submitted_at": at(4)},
        ]
        out = sv.progress_for(rows, START)
        self.assertTrue(out[1]["solved"])
        self.assertFalse(out[2]["solved"])
        self.assertEqual(out[2]["wrong"], 1)

if __name__ == "__main__":
    unittest.main()
