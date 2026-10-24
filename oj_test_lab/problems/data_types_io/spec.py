"""Specification constants for `data-types-io`.

Input format (LF line endings, UTF-8/ASCII, no spaces, exactly 5 lines, the
last line also terminated by one LF):

    X   integer,  -10^9  <= X <= 10^9
    Y   integer,  -10^18 <= Y <= 10^18
    C   one ASCII letter [A-Za-z]
    F   decimal,  -10^6  <= F <= 10^6, at most 2 fractional digits
    D   decimal,  -10^9  <= D <= 10^9, at most 9 fractional digits

Numeric representation / rounding policy (MVP):
    * F and D are plain decimal literals (no exponent, no '+', no NaN/Inf,
      no negative zero, no leading zeros).
    * The reference solution works on exact decimals (never binary floats):
      it pads the literal with zeros to exactly 2 (F) / 9 (D) fractional
      digits. Because the input never has more fractional digits than the
      output, NO rounding ever happens.
    * To keep results identical in C++ (`float` for F, `double` for D) the
      validator additionally requires every value to be EXACTLY representable
      in IEEE-754 binary32 (F) / binary64 (D). So F is a multiple of 1/4 and D
      (in practice) a multiple of 1/512, with bounded magnitude. This
      removes all binary rounding ambiguity from the MVP tests.
    * Arbitrary decimals such as 0.1 are intentionally NOT generated; their
      printed form depends on language/library and is out of MVP scope.
"""

X_MIN, X_MAX = -(10**9), 10**9
Y_MIN, Y_MAX = -(10**18), 10**18
F_MIN, F_MAX = -(10**6), 10**6
D_MIN, D_MAX = -(10**9), 10**9
F_PLACES = 2
D_PLACES = 9
LINE_COUNT = 5
