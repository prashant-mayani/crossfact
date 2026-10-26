"""Results as a Markdown table (for docs/EVAL.md). Correct, wrong and not sure
always appear together."""
from __future__ import annotations


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _ci(ci) -> str:
    return "" if not ci or ci[0] is None else f" ({100 * ci[0]:.0f}-{100 * ci[1]:.0f})"


def _secs(x: float | None) -> str:
    return "-" if x is None else f"{x:.0f} s" if x >= 10 else f"{x:.1f} s"


def markdown(res: dict) -> str:
    m = res["meta"]
    versions = m.get("versions") or [{"code": m.get("code_version", "?"), "registry": "?"}]
    code = ", ".join(f"`{v['code']}`/`{v['registry']}`" for v in versions)
    lines = [f"### `{res['run_id']}`", "",
             f"{res['questions']} of {m['questions']} questions graded from `{m['dataset']}` "
             f"(sha256 `{m['dataset_sha256'][:12]}`), started {m['started']}, code/registry {code}"
             + (" **(mixed versions: allowed with --allow-version-change)**" if len(versions) > 1 else "")
             + f". Grader `{m['grader']}`"
             + ("" if m.get("grader_template_pinned", True) else " **(with a local grader template that is not the "
                "pinned simple-evals file)**")
             + f". Closed-book; every system was told: *{m['instruction']}*", "",
             "| system | graded | correct | wrong | not sure | accuracy when it answered | F1 | errors | "
             "median latency | calls per question |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    ungraded = 0
    for name, s in res["systems"].items():
        ungraded += s.get("ungraded", 0)
        lat = _secs(s["latency_p50"]) + (f" (n={s['latency_n']})" if s["latency_n"] < s["n"] else "")
        right = f"{_pct(s['correct_rate'])}{_ci(s.get('correct_rate_ci'))}"
        wrong = f"{_pct(s['incorrect_rate'])}{_ci(s.get('incorrect_rate_ci'))}"
        lines.append(f"| {name} | {s['n']} | {right} | {wrong} | {_pct(s['not_attempted_rate'])} | "
                     f"{_pct(s['attempted_accuracy'])} | {100 * s['f1']:.1f}{_ci(s.get('f1_ci'))} | {s['errors']} | "
                     f"{lat} | {s['calls_per_question']:.1f} |")
    lines += ["", "Ranges are 95% bootstrap intervals over questions. Errors are answers that failed three times "
              "(provider trouble), counted and not scored. Latency counts only answers that made no cached calls "
              "(n shown when that is fewer than all) and leaves out time spent waiting on rate limits."]
    if ungraded:
        lines += [f"{ungraded} answers are not graded yet (the grader failed or was paused); the next run grades "
                  "them."]
    if res.get("paired"):
        lines += ["", f"Against **{res['reference']}** (the reference), on the questions both answered:", "",
                  "| system | questions | change in correct | p | change in wrong | p | lost | found |",
                  "|---|---|---|---|---|---|---|---|"]
        for name, p in res["paired"].items():
            s = res["systems"][name]
            lines.append(f"| {name} | {p['n']} | {100 * p['correct_delta']:+.1f} pp | {p['correct_p']:.3f} | "
                         f"{100 * p['incorrect_delta']:+.1f} pp | {p['incorrect_p']:.3f} | "
                         f"{_pct(s.get('oracle_gap'))} | {_pct(s.get('gain_rate'))} |")
        lines += ["", "p: exact McNemar test on the paired outcomes. Fewer wrong answers bought only by abstaining "
                  "shows up as a drop in correct answers too. Lost: a participant (a model the system asked, or "
                  "self-check's first answer) was right and the final answer was not. Found: the final answer was "
                  "right and no participant was."]
    return "\n".join(lines) + "\n"
