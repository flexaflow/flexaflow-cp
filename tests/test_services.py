import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import datetime as dt
import services as s

UTC = dt.timezone.utc
start = dt.datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
end = dt.datetime(2026, 11, 21, 13, 0, tzinfo=UTC)

# status
assert s.contest_status(None, None) == "not_configured"
assert s.contest_status(start, end, start - dt.timedelta(seconds=1)) == "before"
assert s.contest_status(start, end, start) == "running"
assert s.contest_status(start, end, end) == "ended"

# validation
good = {"problems": [{"title": "A+B", "statement": "sum",
        "tests": [{"input": "1 2\n", "output": "3\n", "sample": True}, {"input": "5 5\n", "output": "10\n"}]}]}
p = s.validate_questions(good)
assert p[0]["time_limit_sec"] == 2.0 and p[0]["mem_limit_mb"] == 256
assert p[0]["tests"][0]["sample"] == 1 and p[0]["tests"][1]["sample"] == 0
for bad in [[], {"problems": []}, {"problems": [{"title": "x", "statement": "y", "tests": []}]},
            {"problems": [{"title": "x", "statement": "y", "tests": [{"input": "1"}]}]},
            {"problems": [{"title": "", "statement": "y", "tests": [{"input": "", "output": ""}]}]},
            {"problems": [{"title": "x", "statement": "y", "time_limit_sec": -1, "tests": [{"input": "", "output": ""}]}]}]:
    try:
        s.validate_questions(bad); raise SystemExit(f"should reject {bad}")
    except s.QuestionFileError:
        pass

# leaderboard
users = [{"id": 1, "username": "a", "full_name": "A"}, {"id": 2, "username": "b", "full_name": "B"},
         {"id": 3, "username": "c", "full_name": None}]
def sub(uid, pid, verdict, minutes):
    return {"user_id": uid, "problem_id": pid, "verdict": verdict,
            "submitted_at": (start + dt.timedelta(minutes=minutes)).isoformat()}
subs = [
    sub(1, 1, "Wrong Answer", 5),
    sub(1, 1, "Accepted", 30),     # solved at 30 + 1 wrong*20 = 50
    sub(1, 2, "Accepted", 40),     # +40  => total 90, solved 2
    sub(2, 1, "Accepted", 10),     # solved 1, penalty 10
    sub(2, 2, "Accepted", 10),     # solved 2, penalty 20 -> should rank above a
    sub(3, 1, "Accepted", 200),    # after... still inside window? 10:00+200min=13:20 > end -> excluded
    sub(3, 1, "Pending", 1),       # ignored
    sub(1, 1, "Accepted", -5),     # before start -> excluded (and already solved anyway)
]
board = s.compute_leaderboard(users, sorted(subs, key=lambda x: x["submitted_at"]), start, end)
got = [(r["username"], r["solved"], r["penalty"]) for r in board]
print(got)
assert got[0] == ("b", 2, 20), got
assert got[1] == ("a", 2, 90), got
assert got[2] == ("c", 0, 0), got
assert board[0]["rank"] == 1
print("ALL SERVICES TESTS PASSED")
