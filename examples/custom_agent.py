"""Run with --agent examples.custom_agent:MyAgent (from the project root)."""

from atc_bench.agents import ReferenceAgent


class MyAgent:
    """Replace act with an LLM, search algorithm, or your own policy.

    Observation is a JSON-compatible dictionary. Return up to 100 commands;
    an empty list waits for the next simulation tick. No environment access
    or API key is required. This example delegates to the reference policy.
    """

    def __init__(self):
        self.policy = ReferenceAgent()

    def act(self, observation):
        return self.policy.act(observation)
