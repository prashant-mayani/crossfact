"""Crossfact's measurement harness: does the consensus answer beat the baselines?

    python -m bench run --config bench/configs/free.yaml --limit 200
    python -m bench report --run <run id>

Every system answers the same questions closed-book, with the same instruction
to say "I don't know" rather than guess, and every answer is graded the same
way: correct, incorrect, or not attempted, reported side by side. See
docs/EVAL.md for the method and what the numbers can and can't say.
"""
