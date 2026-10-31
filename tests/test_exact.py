"""The exact route: what it computes, and (as important) what it leaves to the models."""
import re
import time
from datetime import date

import pytest

from app.exact import solve


@pytest.mark.parametrize(("question", "answer", "working"), [
    ("What is 2+3?", "5", "2 + 3 = 5"),
    ("what's 487 × 36", "17532", "487 × 36 = 17532"),
    ("What is 9,876 − 4,789?", "5087", None),
    ("2 + 3 * 4", "14", None),
    ("(2 + 3) * 4", "20", None),
    ("10 - 3 - 2", "5", None),
    ("2^3^2", "512", None),                       # right-associative, as in mathematics
    ("-2^2", "-4", None),
    ("2^-2", "0.25", None),
    ("What is 2 to the power of 20?", "1048576", "2^20 = 1048576"),
    ("2**10", "1024", None),
    ("3 squared", "9", None),
    ("calculate 12 times 12", "144", None),
    ("how much is 12 x 12?", "144", None),
    ("what does 2+3 equal", "5", None),
    ("what is 10 / 4", "2.5", None),
    ("what is 0.1 + 0.2", "0.3", None),           # exact, not 0.30000000000000004
    ("1/3 + 1/6", "0.5", None),
    ("100 / 7", "≈ 14.28571429", "100 ÷ 7 = 100/7 ≈ 14.28571429"),
    ("What is 15% of 840?", "126", "15% of 840 = 126"),
    ("12.5 percent of 80", "10", None),
    ("What is the square root of 15,129?", "123", "√15,129 = 123"),
    ("sqrt(2)", "≈ 1.414213562", None),
    ("√16 + 9", "13", None),
    ("the cube root of 27", "3", None),
    ("8^(1/3)", "2", None),
    ("what is 7!", "5040", "7! = 5040"),
    ("7 factorial", "5040", None),
    ("the factorial of 10", "3628800", None),
    ("17 mod 5", "2", None),
    ("What is the remainder when 2 to the power of 10 is divided by 7?", "2", "2^10 mod 7 = 2"),
    ("What is the greatest common divisor of 462 and 1071?", "21", "gcd(462, 1071) = 21"),
    ("lcm of 4, 6 and 10", "60", None),
    ("gcd(12, 18)", "6", None),
    ("what is 10 / 0", "undefined (division by zero)", None),
    ("what is 10^30", "1" + "0" * 30, None),
])
def test_arithmetic(question, answer, working):
    r = solve(question)
    assert r is not None and r.kind == "arithmetic" and r.answer == answer
    if working:
        assert r.working == working


@pytest.mark.parametrize(("question", "answer"), [
    ("5 km in miles", "≈ 3.107 miles"),                         # rounded as people write it
    ("What is 5 miles in kilometers?", "8.04672 kilometers"),
    ("How many feet are in a mile?", "5280 feet"),
    ("how many cm in 3 inches", "7.62 centimeters"),
    ("1 inch in cm", "2.54 centimeters"),
    ("60 mph in km/h", "96.56064 kilometers per hour"),
    ("how many ounces in a pound", "16 ounces"),
    ("how many cups in a gallon", "16 US cups"),                 # US units, and the answer says so
    ("how many fluid ounces in a cup", "8 US fluid ounces"),
    ("how many seconds in a day", "86400 seconds"),
    ("how many teaspoons in a tablespoon", "3 US teaspoons"),
    ("convert 100 °F to °C", "≈ 37.78 °C"),
    ("1 km in inches", "≈ 39370.08 inches"),                    # at least two decimals
    ("1 oz in kg", "≈ 0.02835 kilograms"),                      # at least four significant digits
    ("100 degrees celsius in fahrenheit", "212 °F"),
    ("-40 f to c", "-40 °C"),
    ("0 K in C", "-273.15 °C"),
    ("1 acre in square meters", "≈ 4046.86 square meters"),    # the exact 4046.8564224 is in the working
    ("1 kWh in kilojoules", "3600 kilojoules"),
    ("1 atm in kPa", "101.325 kilopascals"),
])
def test_unit_conversions_use_exact_definitions(question, answer):
    r = solve(question)
    assert r is not None and r.kind == "units" and r.answer == answer


def test_a_conversion_shows_the_definitions_it_used_and_the_unrounded_value():
    assert solve("5 km in miles").working == ("5 km ≈ 3.106855961 mi (by definition, 1 km = 1000 m; "
                                             "1 mi = 1609.344 m)")
    assert solve("convert 100 °F to °C").working == "100 °F = 340/9 °C ≈ 37.77777778 °C"


@pytest.mark.parametrize(("question", "answer"), [
    ("how many days between 2026-01-01 and 2026-03-01", "59 days"),
    ("How many days are there between January 1, 2026 and March 1, 2026?", "59 days"),
    ("how many weeks between january 1, 2026 and march 1, 2026", "8 weeks and 3 days"),
    ("What date is 90 days after October 30, 2026?", "Thursday, January 28, 2027"),
    ("2026-10-30 plus 2 weeks", "Friday, November 13, 2026"),
    ("30 days before 1 March 2024", "Wednesday, January 31, 2024"),   # through February 29: a leap year
    ("what day of the week was July 4, 1776", "Thursday"),
    ("which day of the week was the 4th of july 1776", "Thursday"),
    ("what day is 2026-12-25", "Friday"),
    ("is 2100 a leap year", "No"),
    ("was 2000 a leap year", "Yes"),
])
def test_date_arithmetic_on_the_gregorian_calendar(question, answer):
    r = solve(question)
    assert r is not None and r.kind == "dates" and r.answer == answer


def test_a_span_of_days_says_how_it_counted():
    assert "60 counting both the first and the last day" in solve("days between 2026-01-01 and 2026-03-01").working


@pytest.mark.parametrize("question", [
    # not computation at all
    "What is the capital of France?", "Who wrote Hamlet?", "how many people live in Paris",
    "What is the sum of the whole numbers from 1 to 50?", "what is 3.14", "what is -5", "2(3)",
    # dates, names and idioms written with / or -
    "What is 9/11?", "what is 24/7", "what is 7-11", "what is 50/50", "what is 1-800-273-8255",
    "what is 2026-10-30",
    # ambiguous on purpose
    "what is 0x10", "what is 0^0", "-7 mod 3", "what is 2+3!", "what is 5!!", "what is 200 + 10%",
    "what is 2+3 in binary", "what is 5 in roman numerals", "what is (-8)^(1/3)", "gcd(12,180)",
    "how many ounces in a cup",          # weight or fluid ounces?
    "how many days in a year", "how many seconds in a month", "how many calories in a kilojoule",
    "how many MB in a GB", "5 tons in kg", "5 kg in miles", "-500 c to f",
    "how many days between 03/04/2026 and 05/06/2026",   # month or day first?
    "what day of the week was october 12, 1492",         # Julian or Gregorian?
    "how many days until christmas", "what day is it", "what is 30 days from today",   # without the asker's date
    # too big to be worth computing
    "what is 9^9^9", "what is 2^1000000",
    # two lines, or a long text, is never a calculation
    "2+3\nand then explain", "what is " + "1+" * 200 + "1",
])
def test_anything_unsure_is_left_to_the_models(question):
    assert solve(question) is None


def test_the_same_question_gets_the_same_answer_at_once():
    first = solve("What is 487 multiplied by 36?")
    t0 = time.perf_counter()
    for _ in range(1000):
        assert solve("What is 487 multiplied by 36?") == first
    assert time.perf_counter() - t0 < 0.5  # cached: a thousand answers in well under a second


def test_huge_results_are_shown_in_scientific_notation():
    r = solve("what is 1000!")
    assert r.answer == "≈ 4.023872601e+2567"


def _matches(gold: str, answer: str) -> bool:
    """The gold answer appears in ours as a whole word or number, or ours rounds to it."""
    g, a = gold.lower().replace(",", "").strip(), answer.lower().replace(",", "").lstrip("≈ ")
    if re.search(rf"(?<![\w.]){re.escape(g)}(?![\w]|\.\d)", a):
        return True
    num = re.match(r"-?\d+(?:\.\d+)?", a)
    try:
        return num is not None and abs(float(num.group()) - float(g)) <= 0.5 * 10 ** -len(g.partition(".")[2])
    except ValueError:
        return False


def test_every_answer_it_gives_on_the_bundled_exact_set_is_right():
    """bench/sets/exact.yaml: thirty questions with one computed answer, written for the
    models. The route takes the ones it can read (the rest go to the models); a wrong
    answer here would be a bug, never a trade-off."""
    import os

    import yaml

    from tests.conftest import ROOT

    with open(os.path.join(ROOT, "bench", "sets", "exact.yaml")) as f:
        questions = yaml.safe_load(f)["questions"]
    taken = [(q, solve(q["question"])) for q in questions]
    taken = [(q, r) for q, r in taken if r is not None]
    assert len(taken) >= 13
    wrong = [(q["id"], q["answer"], r.answer) for q, r in taken if not _matches(str(q["answer"]), r.answer)]
    assert wrong == []


# ---- relative to the asker's own date (their browser sends it) ----------------------------

TODAY = date(2026, 10, 30)   # a Friday, in New York; it was already October 31 in UTC


@pytest.mark.parametrize(("question", "answer"), [
    ("How many days until Christmas?", "56 days"),               # 55 by the server's UTC date
    ("how long until halloween", "1 day"),
    ("how many weeks until new year's", "9 weeks"),
    ("how many days since July 4", "118 days"),
    ("how many days since christmas", "309 days"),               # the last one: December 25, 2025
    ("how many days until february 29", "487 days"),             # the next one is in 2028
    ("how many days until christmas 2027", "421 days"),
    ("how many days between today and christmas", "56 days"),
    ("What's today's date?", "Friday, October 30, 2026"),
    ("what day is it", "Friday, October 30, 2026"),
    ("what day is tomorrow", "Saturday, October 31, 2026"),
    ("what was the date yesterday", "Thursday, October 29, 2026"),
    ("90 days from today", "Thursday, January 28, 2027"),
    ("in 2 weeks", "Friday, November 13, 2026"),
    ("10 days ago", "Tuesday, October 20, 2026"),
    ("what day of the week is christmas", "Friday"),
])
def test_dates_relative_to_the_asker_are_exact_when_their_date_is_known(question, answer):
    r = solve(question, TODAY, "America/New_York")
    assert r is not None and r.answer == answer


def test_the_working_names_today_and_the_time_zone():
    assert solve("how many days until christmas", TODAY, "America/New_York").working == (
        "From today, Friday, October 30, 2026 (America/New_York), to Friday, December 25, 2026: 56 days")
    assert solve("halloween", TODAY) is None and solve("how many days until 2025-01-01", TODAY) is None
    assert solve("how many days until my birthday", TODAY) is None
    assert solve("how many days until halloween", date(2026, 10, 31)).answer == "0 days (it's today)"


def test_a_cached_answer_is_never_reused_on_another_day():
    assert solve("how many days until christmas", TODAY).answer == "56 days"
    assert solve("how many days until christmas", date(2026, 10, 31)).answer == "55 days"
