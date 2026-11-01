"""Convert temperatures.

python3 temps.py 21C 70F
21 C = 69.8 F
70 F = 21.1 C
"""

import sys


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def convert(text: str) -> str:
    value, unit = float(text[:-1]), text[-1].upper()
    if unit == "C":
        return f"{value:g} C = {c_to_f(value):.1f} F"
    if unit == "F":
        return f"{value:g} F = {f_to_c(value):.1f} C"
    raise ValueError(f"unknown unit {unit!r}: use C or F")


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(convert(arg))
