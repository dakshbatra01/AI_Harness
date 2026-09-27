"""A tiny buggy repository plus a scripted model trajectory that fixes it (offline e2e tests)."""
from __future__ import annotations

import subprocess
from pathlib import Path

FILES = {
    "durations/__init__.py": "from .core import format_duration, parse_duration\n\n__all__ = ['parse_duration', 'format_duration']\n",
    "durations/core.py": '''"""Duration helpers."""
import re

_UNITS = {"h": 3600, "m": 60, "s": 1}
_TOKEN = re.compile(r"(\\d+)\\s*([hms])")


def parse_duration(text):
    """Parse strings like '1h30m' or '45s' into a number of seconds."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty duration")
    total = 0
    for value, unit in _TOKEN.findall(text.lower()):
        total = int(value) * _UNITS[unit]
    return total


def format_duration(seconds):
    """Inverse of parse_duration for whole seconds."""
    parts = []
    for unit, size in (("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return "".join(parts) or "0s"
''',
    "tests/__init__.py": "",
    "tests/test_durations.py": '''import unittest

from durations import format_duration, parse_duration


class TestParse(unittest.TestCase):
    def test_single_unit(self):
        self.assertEqual(parse_duration("45s"), 45)
        self.assertEqual(parse_duration("2h"), 7200)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            parse_duration("")


class TestFormat(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_duration(5400), "1h30m")
        self.assertEqual(format_duration(0), "0s")


if __name__ == "__main__":
    unittest.main()
''',
    "README.md": "# durations\n\nParse and format human durations.\n",
}

ISSUE = """parse_duration ignores all but the last component

`parse_duration("1h30m")` returns 1800 instead of 5400. It looks like only the last unit
in a compound duration is counted, e.g. `parse_duration("2m5s")` gives 5.

Expected: every component is summed (1h30m -> 5400 seconds).
"""


def make_fixture_repo(dest: Path, git: bool = True) -> tuple[Path, str]:
    dest.mkdir(parents=True, exist_ok=True)
    for rel, content in FILES.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if git:
        env_args = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
        subprocess.run(["git", *env_args, "add", "-A"], cwd=dest, check=True)
        subprocess.run(["git", *env_args, "commit", "-q", "-m", "init"], cwd=dest, check=True)
    return dest, ISSUE


SCRIPTED_SOLUTION = [
    'THOUGHT: Find where parse_duration is defined.\n<tool name="search_code">\n<query>parse_duration</query>\n<mode>symbol</mode>\n</tool>',
    'THOUGHT: Read the implementation.\n<tool name="read_file">\n<path>durations/core.py</path>\n</tool>',
    'THOUGHT: The loop assigns instead of accumulating. Reproduce first.\n<tool name="write_file">\n<path>.harness_scratch/repro.py</path>\n'
    '<content>\nfrom durations import parse_duration\nassert parse_duration("1h30m") == 5400, parse_duration("1h30m")\n'
    'assert parse_duration("2m5s") == 125\nprint("ok")\n</content>\n</tool>\n'
    '<tool name="run_command">\n<command>python .harness_scratch/repro.py</command>\n</tool>',
    'THOUGHT: Reproduced (AssertionError). Record the hypothesis and fix.\n<tool name="update_plan">\n'
    '<hypothesis>parse_duration overwrites `total` on each token instead of summing</hypothesis>\n</tool>\n'
    '<tool name="edit_file">\n<path>durations/core.py</path>\n<old_text>\n        total = int(value) * _UNITS[unit]\n</old_text>\n'
    '<new_text>\n        total += int(value) * _UNITS[unit]\n</new_text>\n</tool>',
    'THOUGHT: Add a regression test and run checks.\n<tool name="edit_file">\n<path>tests/test_durations.py</path>\n<old_text>\n'
    '    def test_empty_raises(self):\n</old_text>\n<new_text>\n    def test_compound(self):\n'
    '        self.assertEqual(parse_duration("1h30m"), 5400)\n        self.assertEqual(parse_duration("2m5s"), 125)\n\n'
    '    def test_empty_raises(self):\n</new_text>\n</tool>\n'
    '<tool name="run_command">\n<command>python .harness_scratch/repro.py</command>\n</tool>\n'
    '<tool name="run_tests">\n<tests>tests/test_durations.py</tests>\n</tool>',
    'THOUGHT: All green. Submit.\n<tool name="submit">\n<summary>parse_duration assigned instead of accumulating each component; now sums them. '
    'Added regression test.</summary>\n<test_commands>\npython -m unittest -v tests.test_durations\n</test_commands>\n'
    '<reproducer>python .harness_scratch/repro.py</reproducer>\n</tool>',
]
