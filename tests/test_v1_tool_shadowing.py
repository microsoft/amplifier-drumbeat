"""The measurement behind contracts/agent-binding.v1.md section 10's boundary.

A claim about another project's behavior rots the moment that project changes.
This file keeps the claim GREEN instead of remembered: it drives the REAL
``amplifier_agent`` library -- the same one every turn imports -- and asserts
exactly what it does with a caller tool whose name collides with a built-in.

Measured against ``amplifier-agent 1.0.0a1`` on 2026-09-07:

    AgentError(code='invalid_input',
               message='Duplicate tool name: bash.',
               remedy='Give caller and MCP tools names distinct from the '
                      'built-in tools.')

That refusal is why drumbeat's bounded shell tool is named ``run_command`` and
not ``bash``, why the built-in ``bash`` tool's unbounded result is a recorded
GAP rather than a solved problem, and why section 11's rotate-on-refusal
backstop is not optional. If this test ever goes red because the library
started ALLOWING the shadow, that is very good news and the contract's "Known
gap" should be revisited -- the fix belongs in ``src/drumbeat/``, not here.

No network and no real credential: ``create_agent`` resolves configuration and
assembles the tool registry locally, and the duplicate is refused during that
assembly. The placeholder key exists only so provider selection gets far enough
to reach tool registration.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

from drumbeat import tool_ceiling

_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {"command": {"type": "string", "minLength": 1}},
    "required": ["command"],
}

# The library's own built-in tool names (docs/concepts/tools.md). Every one of
# these is reserved against a caller tool.
BUILT_IN_NAMES = (
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "bash",
    "web_fetch",
    "web_search",
    "delegate",
)


async def _handler(arguments: dict, context: object) -> str:
    return "unreachable -- construction never gets this far in these tests"


def _construct(name: str) -> BaseException | None:
    """Build an agent with one caller tool called ``name``.

    Returns the exception ``create_agent`` raised, or ``None`` when the agent
    constructed successfully.
    """
    from amplifier_agent import AgentOptions, Tool, create_agent

    async def go() -> BaseException | None:
        tool = Tool(
            name=name,
            description="a caller tool deliberately colliding with a built-in",
            input_schema=dict(_SCHEMA),
            handler=_handler,
        )
        with TemporaryDirectory() as storage:
            options = AgentOptions(
                provider="openai",
                model="gpt-5.6-luna",
                tools=[tool],
                storage=storage,
                approvals="allow",
                tool_error_policy="continue",
            )
            try:
                agent = await create_agent(options)
            except BaseException as exc:  # noqa: BLE001 - the exception IS the result
                return exc
            async with agent:
                return None

    # A clean environment: an operator's own $AMPLIFIER_AGENT_CONFIG (layer 1 of
    # drumbeat's merge) would otherwise decide this test's outcome, and a
    # missing credential would fail construction before tools are ever
    # assembled -- neither is what is being measured here.
    env = {k: v for k, v in os.environ.items() if not k.startswith("AMPLIFIER_AGENT_")}
    env["OPENAI_API_KEY"] = "sk-placeholder-never-sent-anywhere"
    with mock.patch.dict(os.environ, env, clear=True):
        return asyncio.run(go())


class TestCallerToolCannotShadowABuiltIn(unittest.TestCase):
    def test_a_caller_tool_named_bash_is_refused_at_construction(self) -> None:
        exc = _construct("bash")
        self.assertIsNotNone(
            exc,
            "amplifier-agent ACCEPTED a caller tool named 'bash'. If that is "
            "real, drumbeat can finally bound the built-in shell tool's result "
            "-- revisit contracts/agent-binding.v1.md section 10 and its "
            "'Known gap'.",
        )
        assert exc is not None
        self.assertEqual(getattr(exc, "code", None), "invalid_input")
        self.assertIn("Duplicate tool name", str(exc))
        self.assertIn("bash", str(exc))

    def test_read_file_is_reserved_the_same_way(self) -> None:
        """So is every other built-in -- the ceiling reaches none of them."""
        exc = _construct("read_file")
        self.assertIsNotNone(exc)
        assert exc is not None
        self.assertIn("Duplicate tool name", str(exc))

    def test_drumbeats_own_tool_name_is_accepted(self) -> None:
        """``run_command`` collides with nothing, which is why it is the name."""
        self.assertNotIn(tool_ceiling.TOOL_NAME, BUILT_IN_NAMES)
        self.assertIsNone(_construct(tool_ceiling.TOOL_NAME))

    def test_agent_options_has_no_way_to_disable_a_built_in(self) -> None:
        """The other half of the gap: the built-in cannot be removed either.

        ``AgentOptions`` is a closed list (docs/concepts/agents.md: "That list
        is closed"). If a tool-filtering field ever appears, this goes red and
        the gap can be closed properly rather than steered around with
        advisory instructions.
        """
        from dataclasses import fields

        from amplifier_agent import AgentOptions

        names = {f.name for f in fields(AgentOptions)}
        self.assertEqual(
            names,
            {
                "provider",
                "model",
                "instructions",
                "tools",
                "skills",
                "mcp_servers",
                "storage",
                "approvals",
                "tool_error_policy",
            },
        )
        for filter_field in ("allowed_tools", "disabled_tools", "builtin_tools"):
            self.assertNotIn(filter_field, names)


class TestTheWorkerRegistersTheBoundedTool(unittest.TestCase):
    """The tool drumbeat actually hands the library, assembled for real."""

    def test_worker_builds_exactly_one_caller_tool_named_run_command(self) -> None:
        from drumbeat import agent_worker

        tools = agent_worker._caller_tools({"cwd": os.getcwd(), "run_dir": None})
        self.assertEqual([t.name for t in tools], [tool_ceiling.TOOL_NAME])
        schema = tools[0].input_schema
        # Same argument shape as the built-in bash tool, so a model that knows
        # one knows the other.
        self.assertEqual(sorted(schema["properties"]), ["command", "timeout"])
        self.assertEqual(schema["required"], ["command"])
        self.assertEqual(
            schema["properties"]["timeout"]["maximum"], tool_ceiling.MAX_TIMEOUT_S
        )

    def test_the_handler_applies_the_ceiling_to_a_real_command(self) -> None:
        from drumbeat import agent_worker

        with TemporaryDirectory() as tmp:
            run_dir = os.path.join(tmp, "run")
            tools = agent_worker._caller_tools(
                {
                    "cwd": tmp,
                    "run_dir": run_dir,
                    "tool_result_ceiling_bytes": 512,
                }
            )
            context = mock.Mock(call_id="call-xyz")
            text = asyncio.run(
                tools[0].handler({"command": "python3 -c \"print('z'*50000)\""}, context)
            )

            self.assertLess(len(text.encode("utf-8")), 900)
            self.assertIn("[drumbeat: output truncated -- kept 512 of ", text)
            written = os.path.join(tmp, "run", tool_ceiling.TOOL_OUTPUT_DIRNAME, "call-xyz.txt")
            self.assertTrue(os.path.isfile(written))
            self.assertGreater(os.path.getsize(written), 50_000)

    def test_a_small_command_result_carries_no_note(self) -> None:
        from drumbeat import agent_worker

        with TemporaryDirectory() as tmp:
            tools = agent_worker._caller_tools(
                {"cwd": tmp, "run_dir": os.path.join(tmp, "run")}
            )
            text = asyncio.run(
                tools[0].handler({"command": "echo hi"}, mock.Mock(call_id="c"))
            )
        self.assertIn("hi", text)
        self.assertNotIn("drumbeat: output truncated", text)

    def test_the_preference_instructions_name_both_tools_and_the_reason(self) -> None:
        """Advisory steering, and honest about being the only steering there is."""
        text = tool_ceiling.TOOL_PREFERENCE_INSTRUCTIONS
        self.assertIn(tool_ceiling.TOOL_NAME, text)
        self.assertIn("bash", text)
        self.assertIn("every later turn", text)


if __name__ == "__main__":
    unittest.main()
