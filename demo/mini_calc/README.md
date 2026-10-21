# mini_calc

A tiny calculator used by the CodePilot-Repair offline Web demo.

The demo task asks the agent to fix `add()`: it currently subtracts one from the
sum, so `add(2, 3)` returns `4` instead of `5`. The run uses the real M2
LangGraph with a scripted FakeLLM and a deterministic fake sandbox. It is an
integration/workflow demonstration, not a real-model quality result.
