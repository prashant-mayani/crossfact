"""The exact route: questions answered by computation in code, never by a model.

In mode "auto", a question that is arithmetic, a unit conversion or date
arithmetic is computed here, exactly (rational arithmetic, exact unit
definitions, the Gregorian calendar), and cached. The same question gets the
same answer, at once, every time, and costs no model call and no quota.

The parser is narrow on purpose. A question is taken only when the whole of it
reads as one of the forms below (after a "what is", "calculate" or "convert").
A leftover word, an unknown unit or an ambiguous form sends it to the models
instead: a miss costs a model call, a wrong capture would cost a wrong answer.
Declined on purpose:
  - two numbers of one or two digits joined by / or - with no spaces ("9/11",
    "24/7", "7-11"): as often a date or a name as a sum
  - runs of hyphens between digits ("1-800-273-8255", "2026-10-30") and hex ("0x10")
  - % except "15% of 840" (calculators read "200 + 10%" differently), 0^0,
    remainders of negative numbers, and a trailing "!" after other operators
    ("2+3!": a factorial, or an exclamation?)
  - units with several definitions ("ton", "calorie", "BTU", "MB", "year");
    gallons, quarts, pints, cups and fluid ounces are US units, and the answer says so
  - dates written 03/04/2026 (month or day first?) or before 1583 (Julian or
    Gregorian?); dates relative to today ("days until Christmas") unless the
    asker's own date is known (the browser sends it: the server's clock runs on UTC)

Forms:
  arithmetic  2+3, 487 × 36, 9,876 − 4,789, 2^20, 2 to the power of 10, 3 squared,
              sqrt(2), √16, the square root of 15,129, 7!, 7 factorial, 17 mod 5,
              the remainder when 2^10 is divided by 7, 15% of 840, the gcd (or lcm)
              of 462 and 1071
  units       5 km in miles, convert 100 °F to °C, how many feet are in a mile
  dates       how many days between 2026-01-01 and March 1, 2026; 90 days after
              October 30, 2026; what day of the week was July 4, 1776; is 2100 a
              leap year; and, given the asker's date, what's today's date, how many
              days until Christmas, since July 4, or 90 days from today
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction
from functools import lru_cache

MAX_LEN = 300            # longer questions are never pure computation
MAX_EXPONENT = 100_000   # 9^9^9 would never finish
MAX_DIGITS = 20_000      # nor would a result this long be worth computing
SHOW_DIGITS = 1_000      # integers longer than this are shown in scientific notation
MAX_FACTORIAL = 1_000
SIG = 10                 # significant digits for a result that can't be shown exactly
FIRST_GREGORIAN_YEAR = 1583


@dataclass(frozen=True)
class Exact:
    answer: str    # what the user gets: "5", "≈ 3.106855961 miles", "Thursday"
    working: str   # what was computed, for the transcript: "2 + 3 = 5"
    kind: str      # "arithmetic" | "units" | "dates"


class _Decline(Exception):
    """Not an exact question, or not one this parser can be sure it reads right."""


def solve(question: str, today: date | None = None, where: str = "") -> Exact | None:
    """The computed answer, or None when the question isn't (safely) an exact one.
    today: the asker's own date, when known (questions relative to it are answered
    only then); where: their time zone, named in the working."""
    q = (question or "").strip()
    if not q or len(q) > MAX_LEN or "\n" in q:
        return None
    return _solve(_clean(q), today.isoformat() if today else None, where if today else "")


@lru_cache(maxsize=4096)
def _solve(text: str, today: str | None = None, where: str = "") -> Exact | None:
    day = date.fromisoformat(today) if today else None
    for form in (lambda t: _dates(t, day, where), _units, _arithmetic):
        try:
            hit = form(text)
        except (_Decline, ArithmeticError, ValueError, RecursionError):
            hit = None
        if hit:
            return hit
    return None


# ---- the question's text ------------------------------------------------------------------

_CHARS = str.maketrans({"−": "-", "×": "*", "✕": "*", "⋅": "*", "·": "*", "÷": "/", "²": "^2", "³": "^3",
                        "√": " sqrt ", "’": "'", " ": " ", " ": " ", "`": "", "“": "", "”": "",
                        '"': ""})
_PREFIX = re.compile(r"^(?:(?:please|hey|ok|okay)[ ,]+)?(?:(?:can|could) you (?:please )?(?:tell me )?|tell me )?"
                     r"(?:what(?:'s| is)(?: the value of| the result of)?|whats|how much is|calculate|compute|"
                     r"evaluate|work out|convert)?\s*")
_SUFFIX = re.compile(r"\s*(?:,? please)?\s*(?:=\s*\??|equals\??|\?|\.)*\s*$")
_DOES_EQUAL = re.compile(r"^what does (.+) (?:equal|make|come to)$")


def _clean(q: str) -> str:
    t = re.sub(r"\s+", " ", q.translate(_CHARS).lower()).strip()
    t = _SUFFIX.sub("", t)
    m = _DOES_EQUAL.match(t)
    if m:
        t = m.group(1)
    t = _PREFIX.sub("", t, count=1)
    return _SUFFIX.sub("", t).strip()


# ---- numbers ------------------------------------------------------------------------------

NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+"
_PREC = 60


def _num(s: str) -> Fraction:
    return Fraction(s.replace(",", ""))


def _dec(q: Fraction) -> Decimal:
    with localcontext() as c:
        c.prec = _PREC
        return Decimal(q.numerator) / Decimal(q.denominator)


class _V:
    """A value: exact (a Fraction) while it can be, an approximation (a Decimal) after."""

    __slots__ = ("q", "d")

    def __init__(self, q: Fraction | None = None, d: Decimal | None = None):
        self.q = q
        self.d = d if d is not None else _dec(q)

    @property
    def exact(self) -> bool:
        return self.q is not None

    def is_int(self) -> bool:
        return self.q is not None and self.q.denominator == 1

    def sign(self) -> int:
        x = self.q if self.exact else self.d
        return (x > 0) - (x < 0)


def _digits(n: int) -> int:
    n = abs(n)
    return len(str(n)) if n < 10 ** 4000 else int(math.log10(n)) + 1


def _fmt(v: _V, sig: int = SIG) -> tuple[str, bool]:
    """(text, shown exactly). Integers and short terminating decimals are shown in
    full; anything else to `sig` significant digits."""
    if v.exact:
        q = v.q
        if q.denominator == 1:
            if _digits(q.numerator) <= SHOW_DIGITS:
                return str(q.numerator), True
            return _sci(Decimal(q.numerator), sig), False
        den = q.denominator
        while den % 2 == 0:
            den //= 2
        while den % 5 == 0:
            den //= 5
        if den == 1:  # a terminating decimal
            text = _plain(_dec(q))
            if len(text.split(".")[-1]) <= 30:
                return text, True
    return _approx(v.d, sig), False


def _plain(d: Decimal) -> str:
    text = format(d.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _sci(d: Decimal, sig: int) -> str:
    mant, exp = f"{d:.{sig - 1}e}".split("e")
    mant = mant.rstrip("0").rstrip(".") if "." in mant else mant
    return f"{mant}e{int(exp):+d}"


def _friendly(v: _V) -> tuple[str, bool]:
    """A measurement as people write it: exactly when that's short (2.54, 96.56064),
    else to 4 significant digits and at least 2 decimals (37.78, 3.107, 39370.08)."""
    text, exact = _fmt(v)
    if exact and len(text.partition(".")[2]) <= 6:
        return text, True
    d = v.d
    if d == 0 or abs(d) >= Decimal(10) ** 15 or abs(d) < Decimal("1e-6"):
        return _sci(d, 4), False
    places = max(2, 3 - d.copy_abs().adjusted())
    with localcontext() as c:
        c.prec = _PREC
        return _plain(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)), False


def _approx(d: Decimal, sig: int = SIG) -> str:
    if d == 0:
        return "0"
    if abs(d) >= Decimal(10) ** 15 or abs(d) < Decimal("1e-6"):
        return _sci(d, sig)
    with localcontext() as c:
        c.prec = sig
        return _plain(+d)


# ---- arithmetic ---------------------------------------------------------------------------

_WORD_OPS = [
    (r"\bmultiplied by\b", "*"), (r"\btimes\b", "*"), (r"\bdivided by\b", "/"),
    (r"\bplus\b", "+"), (r"\bminus\b", "-"),
    (r"\b(?:raised )?to the power of\b", "^"), (r"\braised to\b", "^"),
    (r"\bsquared\b", "^2"), (r"\bcubed\b", "^3"), (r"\bmodulo\b", " mod "),
    (r"(?<=[\d)])\s*x\s*(?=[\d(.])", "*"), (r"\*\*", "^"),
]
_TOKEN = re.compile(rf"\s*(?:(?P<num>{NUM})|(?P<op>[-+*/^()!])|(?P<word>mod|sqrt|cbrt))")
_DATE_LIKE = re.compile(r"^\d{1,2}[/-]\d{1,2}$")
_GCD = re.compile(r"^(?:the )?(?P<f>gcd|greatest common divisor|greatest common factor|gcf|highest common factor|hcf|"
                  r"lcm|least common multiple|lowest common multiple)(?: of | ?\()(?P<args>[^()]+?)\)?$")
_REMAINDER = re.compile(r"^(?:the )?remainder (?:when|of) (?P<a>.+?) (?:is )?divided by (?P<b>.+)$")
_PERCENT = re.compile(rf"^(?P<p>{NUM}) ?(?:%|percent) of (?P<n>{NUM})$")
_ROOT = re.compile(rf"^(?:the )?(?P<f>square|cube) root of (?P<n>{NUM})$")
_FACTORIAL = re.compile(r"^(?:(?:the )?factorial of (?P<a>\d+)|(?P<b>\d+) factorial)$")


def _arithmetic(t: str) -> Exact | None:
    if not re.search(r"\d", t):
        return None
    for form in (_percent, _root, _factorial, _gcd_lcm, _remainder):
        hit = form(t)
        if hit:
            return hit
    return _expression(t)


def _done(working: str, v: _V) -> Exact:
    text, exact = _fmt(v)
    if exact:
        return Exact(text, f"{working} = {text}", "arithmetic")
    q = v.q
    as_fraction = f" = {q.numerator}/{q.denominator}" if q is not None and q.denominator != 1 else ""
    return Exact(f"≈ {text}", f"{working}{as_fraction} ≈ {text}", "arithmetic")


def _percent(t: str) -> Exact | None:
    m = _PERCENT.match(t)
    return _done(f"{m['p']}% of {m['n']}", _V(_num(m["p"]) / 100 * _num(m["n"]))) if m else None


def _root(t: str) -> Exact | None:
    m = _ROOT.match(t)
    if not m:
        return None
    if m["f"] == "square":
        return _done(f"√{m['n']}", _sqrt(_V(_num(m["n"]))))
    return _done(f"∛{m['n']}", _cbrt(_V(_num(m["n"]))))


def _factorial(t: str) -> Exact | None:
    m = _FACTORIAL.match(t)
    if not m:
        return None
    n = m["a"] or m["b"]
    return _done(f"{n}!", _fact(_V(_num(n))))


def _gcd_lcm(t: str) -> Exact | None:
    m = _GCD.match(t)
    if not m:
        return None
    args = [a for a in re.split(r"\s*(?:,(?!\d{3}\b)|\band\b)\s*", m["args"].strip()) if a]
    if len(args) < 2 or not all(re.fullmatch(r"\d{1,3}(?:,\d{3})+|\d+", a) for a in args):
        raise _Decline
    nums = [int(a.replace(",", "")) for a in args]
    lcm = m["f"].startswith(("lcm", "least", "lowest"))
    value = math.lcm(*nums) if lcm else math.gcd(*nums)
    return _done(f"{'lcm' if lcm else 'gcd'}({', '.join(map(str, nums))})", _V(Fraction(value)))


def _remainder(t: str) -> Exact | None:
    m = _REMAINDER.match(t)
    if not m:
        return None
    a, ta, _ = _parse(m["a"])
    b, tb, _ = _parse(m["b"])
    return _done(f"{ta} mod {tb}", _mod(a, b))


def _expression(t: str) -> Exact | None:
    if _DATE_LIKE.match(t) or len(re.findall(r"\d-\d", t)) >= 2 or re.search(r"\b0x[0-9a-f]", t):
        raise _Decline
    try:
        v, text, ops = _parse(t, need_op=True)
    except ZeroDivisionError:
        return Exact("undefined (division by zero)", f"{t}: division by zero is undefined", "arithmetic")
    if t.endswith("!") and (t.count("!") > 1 or ops - {"!"}):
        raise _Decline  # "2+3!": a factorial, or an exclamation?
    return _done(text, v)


def _parse(t: str, need_op: bool = False) -> tuple[_V, str, set]:
    for pattern, sub in _WORD_OPS:
        t = re.sub(pattern, sub, t)
    t = t.strip()
    tokens, pos = [], 0
    while pos < len(t):
        m = _TOKEN.match(t, pos)
        if not m:
            raise _Decline
        tokens.append(m["num"] or m["op"] or m["word"])
        pos = m.end()
        while pos < len(t) and t[pos] == " ":
            pos += 1
    p = _Parser(tokens)
    v, text = p.expr()
    if p.i != len(tokens) or (need_op and not p.ops):
        raise _Decline
    return v, text, p.ops


class _Parser:
    """expr := term (('+'|'-') term)*      term := unary (('*'|'/'|'mod') unary)*
    unary := ('-'|'+') unary | power      power := postfix ('^' unary)?   (right-associative)
    postfix := primary '!'?               primary := NUM | '(' expr ')' | ('sqrt'|'cbrt') primary"""

    def __init__(self, tokens: list[str]):
        self.t, self.i, self.depth, self.ops = tokens, 0, 0, set()

    def peek(self) -> str | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> str:
        tok = self.peek()
        if tok is None:
            raise _Decline
        self.i += 1
        return tok

    def expr(self):
        v, text = self.term()
        while self.peek() in ("+", "-"):
            op = self.take()
            w, wt = self.term()
            self.ops.add(op)
            v, text = _arith(op, v, w), f"{text} {op} {wt}"
        return v, text

    def term(self):
        v, text = self.unary()
        while self.peek() in ("*", "/", "mod"):
            op = self.take()
            w, wt = self.unary()
            self.ops.add(op)
            v, text = _arith(op, v, w), f"{text} {({'*': '×', '/': '÷'}).get(op, op)} {wt}"
        return v, text

    def unary(self):
        if self.peek() in ("-", "+"):
            op = self.take()
            v, text = self.unary()
            if op == "-":
                v = _V(-v.q) if v.exact else _V(None, -v.d)
            return v, f"{op}{text}"
        return self.power()

    def power(self):
        v, text = self.postfix()
        if self.peek() == "^":
            self.take()
            e, et = self.unary()
            self.ops.add("^")
            return _pow(v, e), f"{text}^{et}"
        return v, text

    def postfix(self):
        v, text = self.primary()
        if self.peek() == "!":
            self.take()
            self.ops.add("!")
            v, text = _fact(v), f"{text}!"
        return v, text

    def primary(self):
        tok = self.take()
        if tok == "(":
            self.depth += 1
            if self.depth > 40:
                raise _Decline
            v, text = self.expr()
            if self.take() != ")":
                raise _Decline
            self.depth -= 1
            return v, f"({text})"
        if tok in ("sqrt", "cbrt"):
            nxt = self.peek() or ""
            if nxt != "(" and not re.fullmatch(NUM, nxt):
                raise _Decline  # √ binds to the number or bracket right after it, nothing else
            v, text = self.primary()
            self.ops.add(tok)
            return (_sqrt(v), f"√{text}") if tok == "sqrt" else (_cbrt(v), f"∛{text}")
        if re.fullmatch(NUM, tok):
            return _V(_num(tok)), tok
        raise _Decline


def _arith(op: str, a: _V, b: _V) -> _V:
    if op == "mod":
        return _mod(a, b)
    if op == "/" and b.sign() == 0:
        raise ZeroDivisionError
    if a.exact and b.exact:
        if op == "+":
            return _V(a.q + b.q)
        if op == "-":
            return _V(a.q - b.q)
        if op == "*":
            return _V(a.q * b.q)
        return _V(a.q / b.q)
    with localcontext() as c:
        c.prec = _PREC
        if op == "+":
            return _V(None, a.d + b.d)
        if op == "-":
            return _V(None, a.d - b.d)
        if op == "*":
            return _V(None, a.d * b.d)
        return _V(None, a.d / b.d)


def _mod(a: _V, b: _V) -> _V:
    if not (a.is_int() and b.is_int()) or a.q < 0 or b.q <= 0:
        raise _Decline  # remainders of negative numbers differ between conventions
    return _V(a.q % b.q)


def _pow(b: _V, e: _V) -> _V:
    if e.is_int():
        n = e.q.numerator
        if abs(n) > MAX_EXPONENT:
            raise _Decline
        if b.exact:
            if b.q == 0 and n == 0:
                raise _Decline  # 0^0: 1 by one convention, undefined by another
            if b.q == 0 and n < 0:
                raise ZeroDivisionError
            size = max(abs(b.q.numerator), b.q.denominator, 2)
            if abs(n) * math.log10(size) > MAX_DIGITS:
                raise _Decline
            return _V(b.q ** n)
        with localcontext() as c:
            c.prec = _PREC
            return _V(None, b.d ** n)
    if b.sign() <= 0:
        raise _Decline  # a fractional power of a negative number (or of zero) isn't taken here
    if b.exact and e.exact and e.q.denominator <= 12:
        root = _iroot(b.q, e.q.denominator)
        if root is not None:
            return _pow(_V(root), _V(Fraction(e.q.numerator)))
    with localcontext() as c:
        c.prec = _PREC
        return _V(None, b.d ** e.d)


def _iroot_int(n: int, k: int) -> int | None:
    """The exact integer k-th root of n >= 0, or None."""
    if n < 2:
        return n
    x = 1 << -(-n.bit_length() // k)  # a power of two at or above the root
    while True:  # Newton's method, falling to the root from above
        y = ((k - 1) * x + n // x ** (k - 1)) // k
        if y >= x:
            break
        x = y
    return x if x ** k == n else None


def _iroot(q: Fraction, k: int) -> Fraction | None:
    if q < 0 and k % 2 == 0:
        return None
    num, den = _iroot_int(abs(q.numerator), k), _iroot_int(q.denominator, k)
    if num is None or den is None:
        return None
    return Fraction(-num if q < 0 else num, den)


def _sqrt(v: _V) -> _V:
    if v.sign() < 0:
        raise _Decline  # not a real number
    if v.exact:
        r = _iroot(v.q, 2)
        if r is not None:
            return _V(r)
    with localcontext() as c:
        c.prec = _PREC
        return _V(None, v.d.sqrt())


def _cbrt(v: _V) -> _V:
    if v.exact:
        r = _iroot(v.q, 3)
        if r is not None:
            return _V(r)
    with localcontext() as c:
        c.prec = _PREC
        root = abs(v.d) ** (Decimal(1) / 3)
        return _V(None, -root if v.sign() < 0 else root)


def _fact(v: _V) -> _V:
    if not v.is_int() or not 0 <= v.q <= MAX_FACTORIAL:
        raise _Decline
    return _V(Fraction(math.factorial(int(v.q))))


# ---- units --------------------------------------------------------------------------------
# Each row: dimension, size in the base unit (exact, by definition), symbol, singular,
# plural, then other ways people write it. Volumes are in liters. Gallons, quarts,
# pints, cups, fluid ounces, tablespoons and teaspoons are US customary units.

_F = Fraction
_BASE = {"length": "m", "mass": "kg", "volume": "L", "time": "s", "area": "m²", "speed": "m/s", "energy": "J",
         "pressure": "Pa"}
_UNIT_ROWS = [
    ("length", _F("0.001"), "mm", "millimeter", "millimeters", "millimetre", "millimetres"),
    ("length", _F("0.01"), "cm", "centimeter", "centimeters", "centimetre", "centimetres"),
    ("length", _F(1), "m", "meter", "meters", "metre", "metres"),
    ("length", _F(1000), "km", "kilometer", "kilometers", "kilometre", "kilometres"),
    ("length", _F("0.0254"), "in", "inch", "inches"),
    ("length", _F("0.3048"), "ft", "foot", "feet"),
    ("length", _F("0.9144"), "yd", "yard", "yards"),
    ("length", _F("1609.344"), "mi", "mile", "miles"),
    ("length", _F(1852), "nmi", "nautical mile", "nautical miles"),
    ("mass", _F("0.000001"), "mg", "milligram", "milligrams"),
    ("mass", _F("0.001"), "g", "gram", "grams", "gramme", "grammes"),
    ("mass", _F(1), "kg", "kilogram", "kilograms", "kilogramme", "kilogrammes", "kilo", "kilos"),
    ("mass", _F(1000), "t", "tonne", "tonnes", "metric ton", "metric tons"),
    ("mass", _F("0.45359237"), "lb", "pound", "pounds", "lbs"),
    ("mass", _F("0.45359237") / 16, "oz", "ounce", "ounces"),
    ("mass", _F("0.45359237") * 14, "st", "stone", "stone", "stones"),
    ("volume", _F("0.001"), "mL", "milliliter", "milliliters", "millilitre", "millilitres", "ml"),
    ("volume", _F(1), "L", "liter", "liters", "litre", "litres", "l"),
    ("volume", _F("3.785411784"), "US gal", "US gallon", "US gallons", "gallon", "gallons", "gal"),
    ("volume", _F("4.54609"), "imp gal", "imperial gallon", "imperial gallons", "uk gallon", "uk gallons"),
    ("volume", _F("0.946352946"), "US qt", "US quart", "US quarts", "quart", "quarts", "qt"),
    ("volume", _F("0.473176473"), "US pt", "US pint", "US pints", "pint", "pints", "pt"),
    ("volume", _F("0.56826125"), "imp pt", "imperial pint", "imperial pints", "uk pint", "uk pints"),
    ("volume", _F("0.2365882365"), "US cup", "US cup", "US cups", "cup", "cups"),
    ("volume", _F("0.0295735295625"), "US fl oz", "US fluid ounce", "US fluid ounces", "fluid ounce",
     "fluid ounces", "fl oz"),
    ("volume", _F("0.01478676478125"), "US tbsp", "US tablespoon", "US tablespoons", "tablespoon", "tablespoons",
     "tbsp"),
    ("volume", _F("0.00492892159375"), "US tsp", "US teaspoon", "US teaspoons", "teaspoon", "teaspoons", "tsp"),
    ("time", _F("0.001"), "ms", "millisecond", "milliseconds"),
    ("time", _F(1), "s", "second", "seconds", "sec", "secs"),
    ("time", _F(60), "min", "minute", "minutes", "mins"),
    ("time", _F(3600), "h", "hour", "hours", "hr", "hrs"),
    ("time", _F(86400), "day", "day", "days"),
    ("time", _F(604800), "week", "week", "weeks"),
    ("area", _F(1), "m²", "square meter", "square meters", "square metre", "square metres", "sq m", "m^2", "m2"),
    ("area", _F(1_000_000), "km²", "square kilometer", "square kilometers", "square kilometre",
     "square kilometres", "sq km", "km^2", "km2"),
    ("area", _F("0.09290304"), "ft²", "square foot", "square feet", "sq ft", "ft^2", "ft2"),
    ("area", _F("2589988.110336"), "mi²", "square mile", "square miles", "sq mi", "mi^2", "mi2"),
    ("area", _F("4046.8564224"), "acre", "acre", "acres"),
    ("area", _F(10_000), "ha", "hectare", "hectares"),
    ("speed", _F(1), "m/s", "meter per second", "meters per second", "metre per second", "metres per second"),
    ("speed", _F(1000, 3600), "km/h", "kilometer per hour", "kilometers per hour", "kilometre per hour",
     "kilometres per hour", "kph", "kmh"),
    ("speed", _F("1609.344") / 3600, "mph", "mile per hour", "miles per hour"),
    ("speed", _F(1852, 3600), "kn", "knot", "knots"),
    ("speed", _F("0.3048"), "ft/s", "foot per second", "feet per second"),
    ("energy", _F(1), "J", "joule", "joules"),
    ("energy", _F(1000), "kJ", "kilojoule", "kilojoules"),
    ("energy", _F(4184), "kcal", "kilocalorie", "kilocalories"),
    ("energy", _F(3600), "Wh", "watt-hour", "watt-hours", "watt hour", "watt hours"),
    ("energy", _F(3_600_000), "kWh", "kilowatt-hour", "kilowatt-hours", "kilowatt hour", "kilowatt hours"),
    ("pressure", _F(1), "Pa", "pascal", "pascals"),
    ("pressure", _F(1000), "kPa", "kilopascal", "kilopascals"),
    ("pressure", _F(100_000), "bar", "bar", "bar", "bars"),
    ("pressure", _F(101_325), "atm", "atmosphere", "atmospheres"),
    ("pressure", _F("0.45359237") * _F("9.80665") / _F("0.00064516"), "psi", "psi", "psi",
     "pound per square inch", "pounds per square inch"),
    ("pressure", _F(101_325, 760), "torr", "torr", "torr"),
]
_TEMPS = {"°c": "C", "c": "C", "celsius": "C", "centigrade": "C", "degree celsius": "C", "degrees celsius": "C",
          "degrees c": "C", "degree c": "C", "°f": "F", "f": "F", "fahrenheit": "F", "degree fahrenheit": "F",
          "degrees fahrenheit": "F", "degrees f": "F", "degree f": "F", "k": "K", "kelvin": "K", "kelvins": "K"}
_TEMP_SHOWN = {"C": "°C", "F": "°F", "K": "K"}


@dataclass(frozen=True)
class _Unit:
    dim: str
    size: Fraction  # in the dimension's base unit
    symbol: str
    singular: str
    plural: str


def _build_units() -> dict[str, _Unit]:
    out = {}
    for dim, size, symbol, singular, plural, *others in _UNIT_ROWS:
        unit = _Unit(dim, size, symbol, singular, plural)
        for alias in (symbol, singular, plural, *others):
            out.setdefault(alias.lower(), unit)
    return out


_UNITS = _build_units()
_UNIT_FORMS = [
    re.compile(rf"^(?P<n>-?(?:{NUM})) ?(?P<u1>[^\d].*?) (?:to|in|into|as) (?P<u2>[^\d].*?)$"),
    re.compile(rf"^how many (?P<u2>.+?) (?:are |is )?(?:there )?(?:in|make up|make|to) (?:(?:a|an|one) )?"
               rf"(?:(?P<n>{NUM}) ?)?(?P<u1>[^\d].*?)$"),
    re.compile(rf"^(?P<n>-?(?:{NUM})) ?(?P<u1>[^\d].*?) (?:is|are|equals?|makes?) how many (?P<u2>.+?)$"),
]


def _units(t: str) -> Exact | None:
    for form in _UNIT_FORMS:
        m = form.match(t)
        if not m:
            continue
        raw = m["n"] or "1"
        n = -_num(raw[1:]) if raw.startswith("-") else _num(raw)
        t1, t2 = _TEMPS.get(m["u1"].strip()), _TEMPS.get(m["u2"].strip())
        if t1 and t2:
            return _temperature(n, t1, t2)
        u1, u2 = _UNITS.get(m["u1"].strip()), _UNITS.get(m["u2"].strip())
        if not (u1 and u2):
            continue
        if u1.dim != u2.dim or n < 0:
            raise _Decline  # 5 kg in miles, or a negative length: not a conversion
        v = _V(n * u1.size / u2.size)
        shown, shown_exact = _friendly(v)
        text, exact = _fmt(v)
        name = u2.singular if v.q == 1 else u2.plural
        rel = "=" if exact else "≈"
        return Exact(f"{'' if shown_exact else '≈ '}{shown} {name}",
                     f"{_fmt(_V(n))[0]} {u1.symbol} {rel} {text} {u2.symbol}{_basis(u1, u2)}", "units")
    return None


def _basis(*units: _Unit) -> str:
    """The definitions used, so the working can be checked."""
    parts = []
    for u in units:
        if u.size != 1:
            text, exact = _fmt(_V(u.size))
            parts.append(f"1 {u.symbol} {'=' if exact else '≈'} {text} {_BASE[u.dim]}")
    return f" (by definition, {'; '.join(parts)})" if parts else ""


def _temperature(n: Fraction, a: str, b: str) -> Exact:
    kelvin = {"C": n + _F("273.15"), "F": (n - 32) * _F(5, 9) + _F("273.15"), "K": n}[a]
    if kelvin < 0:
        raise _Decline  # below absolute zero: not a temperature
    out = {"C": kelvin - _F("273.15"), "F": (kelvin - _F("273.15")) * _F(9, 5) + 32, "K": kelvin}[b]
    shown, shown_exact = _friendly(_V(out))
    text, exact = _fmt(_V(out))
    unit = _TEMP_SHOWN[b]
    working = f"{_fmt(_V(n))[0]} {_TEMP_SHOWN[a]} = {text} {unit}" if exact else (
        f"{_fmt(_V(n))[0]} {_TEMP_SHOWN[a]} = {out.numerator}/{out.denominator} {unit} ≈ {text} {unit}")
    return Exact(f"{'' if shown_exact else '≈ '}{shown} {unit}", working, "units")


# ---- dates --------------------------------------------------------------------------------

_MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
           "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
           "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12}
_MON = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_FORMS = [
    re.compile(r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})$"),
    re.compile(rf"^(?:the )?(?P<d>\d{{1,2}})(?:st|nd|rd|th)? (?:of )?(?P<mon>{_MON})\.?,? (?P<y>\d{{4}})$"),
    re.compile(rf"^(?P<mon>{_MON})\.? (?:the )?(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,? (?P<y>\d{{4}})$"),
]
_D1, _D2, _DT = (rf"(?P<{k}>[a-z0-9 ,.'-]+?)" for k in ("d1", "d2", "d"))
_BETWEEN = re.compile(rf"^(?:how many |the number of |number of )?(?P<unit>days|weeks) (?:are there |is it )?"
                      rf"(?:between|from) {_D1} (?:and|to|until|till) {_D2}$")
_SHIFT = re.compile(rf"^(?:what (?:date|day) (?:is|was|will (?:it )?be) |what is the date |the date )?"
                    rf"(?P<n>\d{{1,3}}(?:,\d{{3}})+|\d+) (?P<unit>days?|weeks?) "
                    rf"(?P<dir>after|from|before|later than|earlier than) {_DT}$")
_PLUS = re.compile(rf"^{_DT} (?P<op>plus|\+|minus|-) (?P<n>\d+) (?P<unit>days?|weeks?)$")
_WEEKDAY = [
    re.compile(rf"^(?:what|which) day(?: of the week)? (?:is|was|will be|falls on|fell on|will fall on) {_DT}$"),
    re.compile(rf"^(?:the )?day of the week (?:on|for|of|is|was) {_DT}$"),
    re.compile(rf"^(?:what|which) day of the week (?:did|does|will) {_DT} (?:fall on|fall|be)$"),
]
_LEAP = re.compile(r"^(?:is|was|will) (?P<y>\d{4}) (?:be )?a leap year$")
# Relative to the asker's date (only when it is known):
_REL = r"today|tomorrow|yesterday"
_TODAY = re.compile(rf"^(?:(?:what|which) (?:day|date)(?: of the week)? is it(?: today)?"
                    rf"|(?:what|which) (?:day|date)(?: of the week)? (?:is|was|will it be|will be) (?P<r1>{_REL})"
                    rf"|(?:what (?:was|will be) )?(?:the )?(?:date|day)(?: (?P<r2>{_REL}))?"
                    rf"|(?P<r3>{_REL})(?:'?s date)?)$")
_UNTIL = re.compile(rf"^(?:how many (?P<u1>days|weeks)|(?P<u2>days|weeks)|how long|how much time)"
                    rf"(?: are there| is it| is there| are left| left)? (?:until|till|til|to|before) {_DT}$")
_SINCE = re.compile(rf"^(?:how many (?P<u1>days|weeks)|(?P<u2>days|weeks)|how long)"
                    rf"(?: have passed| has it been| is it| have gone by)? since {_DT}$")
_AGO_IN = re.compile(r"^(?:what (?:date|day) (?:is|was|will (?:it )?be) |what (?:is|was) the date |the date )?"
                     r"(?:in (?P<n1>\d+|a|one) (?P<u1>days?|weeks?)|(?P<n2>\d+|a|one) (?P<u2>days?|weeks?) ago)$")
_HOLIDAYS = {"christmas": (12, 25), "christmas day": (12, 25), "xmas": (12, 25), "christmas eve": (12, 24),
             "new year": (1, 1), "new years": (1, 1), "new year's": (1, 1), "new year's day": (1, 1),
             "new years day": (1, 1), "new year's eve": (12, 31), "new years eve": (12, 31),
             "halloween": (10, 31), "valentine's day": (2, 14), "valentines day": (2, 14)}
_MONTH_DAY = [
    re.compile(rf"^(?P<mon>{_MON})\.? (?:the )?(?P<d>\d{{1,2}})(?:st|nd|rd|th)?$"),
    re.compile(rf"^(?P<d>\d{{1,2}})(?:st|nd|rd|th)? (?:of )?(?P<mon>{_MON})$"),
]
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                "November", "December")


def _date(s: str, today: date | None = None, back: bool = False) -> date:
    """A date in the question. Written in full, always. Relative to the asker's
    date ("today", "tomorrow", "Christmas", "March 1": the next one, or the last
    one when back) only when that date is known."""
    s = s.strip()
    for form in _DATE_FORMS:
        m = form.match(s)
        if m:
            groups = m.groupdict()
            month = int(groups["m"]) if groups.get("m") else _MONTHS[groups["mon"]]
            d = date(int(m["y"]), month, int(m["d"]))
            if d.year < FIRST_GREGORIAN_YEAR:
                raise _Decline  # Julian or Gregorian? It depends on the country.
            return d
    s = s.removeprefix("the ")
    m = re.match(r"^(?P<h>.+?),? (?P<y>\d{4})$", s)  # Christmas 2026
    if m and m["h"] in _HOLIDAYS:
        d = date(int(m["y"]), *_HOLIDAYS[m["h"]])
        if d.year < FIRST_GREGORIAN_YEAR:
            raise _Decline
        return d
    if today is None:
        raise _Decline  # relative to a day we don't know
    if s in ("today", "now"):
        return today
    if s in ("tomorrow", "yesterday"):
        return today + timedelta(days=1 if s == "tomorrow" else -1)
    month_day = _HOLIDAYS.get(s)
    for form in _MONTH_DAY:
        m = form.match(s)
        if m and month_day is None:
            month_day = (_MONTHS[m["mon"]], int(m["d"]))
    if month_day is None:
        raise _Decline
    for k in range(9):  # February 29 can be eight years away
        try:
            d = date(today.year - k if back else today.year + k, *month_day)
        except ValueError:
            continue
        if (d <= today) if back else (d >= today):
            return d
    raise _Decline


def long_date(d: date) -> str:
    """Friday, October 30, 2026"""
    return f"{_WEEKDAYS[d.weekday()]}, {_MONTH_NAMES[d.month - 1]} {d.day}, {d.year}"


_long = long_date


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _span(days: int, unit: str) -> str:
    if unit == "weeks":
        w, r = divmod(days, 7)
        return _plural(w, "week") + (f" and {_plural(r, 'day')}" if r else "")
    return _plural(days, "day")


def _shift(start: date, days: int, back: bool) -> Exact:
    end = start - timedelta(days=days) if back else start + timedelta(days=days)
    if end.year < FIRST_GREGORIAN_YEAR:
        raise _Decline
    return Exact(_long(end), f"{_long(start)} {'-' if back else '+'} {_plural(days, 'day')} = {_long(end)}", "dates")


def _from_today(hit: Exact, today: date, now: str) -> Exact:
    """Name today (and the time zone it is today in) in the working."""
    working = hit.working.replace(_long(today), now, 1)
    return Exact(hit.answer, working[:1].upper() + working[1:], hit.kind)


def _dates(t: str, today: date | None = None, where: str = "") -> Exact | None:
    now = (f"today, {_long(today)}" + (f" ({where})" if where else "")) if today else ""
    m = _TODAY.match(t) if today else None
    if m:
        rel = m["r1"] or m["r2"] or m["r3"] or "today"
        d = _date(rel, today)
        verb = "was" if rel == "yesterday" else "is"
        return Exact(_long(d), f"It is {now}" + ("" if rel == "today" else f", so {rel} {verb} {_long(d)}"), "dates")
    m = _BETWEEN.match(t)
    if m:
        d1, d2 = _date(m["d1"], today), _date(m["d2"], today)
        days = abs((d2 - d1).days)
        span = f"From {_long(min(d1, d2))} to {_long(max(d1, d2))}"
        if m["unit"] == "weeks":
            return Exact(_span(days, "weeks"), f"{span}: {_plural(days, 'day')} = {_span(days, 'weeks')}", "dates")
        return Exact(_plural(days, "day"), f"{span}: {_plural(days, 'day')} (counting one end; {days + 1} counting "
                                           "both the first and the last day)", "dates")
    for form, back in ((_UNTIL, False), (_SINCE, True)):
        m = form.match(t) if today else None
        if m:
            unit = m["u1"] or m["u2"] or "days"
            d = _date(m["d"], today, back=back)
            days = (today - d).days if back else (d - today).days
            if days < 0:
                raise _Decline  # "until" a day that has passed, "since" one still to come
            answer = _span(days, unit) + (" (it's today)" if days == 0 else "")
            ends = f"from {_long(d)} to {now}" if back else f"from {now}, to {_long(d)}"
            return Exact(answer, f"{ends[0].upper()}{ends[1:]}: {_plural(days, 'day')}", "dates")
    m = _AGO_IN.match(t) if today else None
    if m:
        n, unit = (m["n1"], m["u1"]) if m["n1"] else (m["n2"], m["u2"])
        days = (1 if n in ("a", "one") else int(n)) * (7 if unit.startswith("week") else 1)
        return _from_today(_shift(today, days, back=not m["n1"]), today, now)
    m = _SHIFT.match(t)
    if m:
        n = int(m["n"].replace(",", "")) * (7 if m["unit"].startswith("week") else 1)
        hit = _shift(_date(m["d"], today), n, m["dir"] in ("before", "earlier than"))
        return _from_today(hit, today, now) if m["d"].strip() in ("today", "now") else hit
    m = _PLUS.match(t)
    if m:
        n = int(m["n"]) * (7 if m["unit"].startswith("week") else 1)
        return _shift(_date(m["d"], today), n, m["op"] in ("minus", "-"))
    for form in _WEEKDAY:
        m = form.match(t)
        if m:
            d = _date(m["d"], today)
            return Exact(_WEEKDAYS[d.weekday()], f"{_long(d)} (Gregorian calendar)", "dates")
    m = _LEAP.match(t)
    if m:
        y = int(m["y"])
        if y < FIRST_GREGORIAN_YEAR:
            raise _Decline
        leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
        why = ("divisible by 400" if y % 400 == 0 else "divisible by 100 but not by 400" if y % 100 == 0
               else "divisible by 4 and not by 100" if y % 4 == 0 else "not divisible by 4")
        return Exact("Yes" if leap else "No",
                     f"{y} is {why}, so it is {'' if leap else 'not '}a leap year (Gregorian calendar)", "dates")
    return None
