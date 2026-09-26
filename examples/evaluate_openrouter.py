"""Run cheap hosted controllers against matched baselines.

Export OPENROUTER_API_KEY, then run python3 -m examples.evaluate_openrouter.
The persistent budget ledger is shared with the dashboard and all CLI runs.
"""
import sys

from .evaluate_lmstudio import main


if __name__ == "__main__":
    raise SystemExit(main(["--agent", "openrouter", "--output", "results/openrouter-evaluation", *sys.argv[1:]]))
