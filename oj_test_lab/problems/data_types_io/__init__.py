"""Problem `data-types-io` — basic data type input/output."""

from pathlib import Path

from ...models import CompareMode, CppCandidate, Problem
from .generator import MIN_COUNT, generate
from .reference import solve
from .validator import validate_input
from .wrong_solutions import WRONG_SOLUTIONS

_CPP = Path(__file__).resolve().parent / "cpp"

PROBLEM = Problem(
    id="data-types-io",
    title="Data Type Input/Output",
    generate=generate,
    validate=validate_input,
    reference=solve,
    wrong_solutions=WRONG_SOLUTIONS,
    cpp_candidates=(
        CppCandidate("cpp-correct-long-long", "Correct: Y read as long long", _CPP / "correct_long_long.cpp"),
        CppCandidate("cpp-wrong-int-y", "Incorrect: Y read as int", _CPP / "wrong_int_y.cpp"),
    ),
    compare_mode=CompareMode.EXACT,
    default_count=28,
    min_count=MIN_COUNT,
    limitations=(
        "F and D only use values exactly representable in binary32/binary64, so C++ float/double output is unambiguous; "
        "arbitrary decimals (e.g. 0.1) are not generated.",
        "Wrong solutions are simulated Python functions, not real compiled C++ programs.",
    ),
)
