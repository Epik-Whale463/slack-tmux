import os
import unittest
from unittest.mock import patch

from telly import brain
from telly.brain import _agent_error_summary, _build_cmd, _merge_opencode_config_content, _parse_output


class ParseOutputTest(unittest.TestCase):
    def test_opencode_text_event(self):
        stdout = """{"type":"step_start","sessionID":"ses_123","part":{"type":"step-start","sessionID":"ses_123"}}
{"type":"text","sessionID":"ses_123","part":{"type":"text","text":"PONG","sessionID":"ses_123"}}
{"type":"step_finish","sessionID":"ses_123","part":{"type":"step-finish","sessionID":"ses_123","cost":0}}
"""

        state = _parse_output(stdout, duration_ms=1234)

        self.assertEqual(state["reply"], "PONG")
        self.assertEqual(state["session_id"], "ses_123")
        self.assertEqual(state["cost_usd"], 0)

    @patch("telly.brain._playwright_mcp_enabled", return_value=True)
    def test_claude_command_includes_playwright_mcp_config(self, _enabled):
        cmd = _build_cmd("/usr/bin/claude", "123.456", "open example.com")

        self.assertIn("--mcp-config", cmd)
        mcp_config = cmd[cmd.index("--mcp-config") + 1]
        self.assertIn('"playwright"', mcp_config)
        self.assertIn('"@playwright/mcp@latest"', mcp_config)

    @patch("telly.brain._playwright_mcp_enabled", return_value=True)
    def test_opencode_config_content_adds_playwright_mcp(self, _enabled):
        config = _merge_opencode_config_content('{"tools":{"bash":true}}')

        self.assertTrue(config["tools"]["bash"])
        self.assertEqual(config["mcp"]["playwright"]["type"], "local")
        self.assertEqual(config["mcp"]["playwright"]["command"][:3], ["npx", "-y", "@playwright/mcp@latest"])

    def test_opencode_config_content_adds_ollama_provider_and_model(self):
        config = _merge_opencode_config_content(
            '{"tools":{"bash":true}}',
            ollama_model="qwen2.5-coder:14b",
            ollama_base_url="http://127.0.0.1:11434/v1",
        )

        self.assertTrue(config["tools"]["bash"])
        self.assertEqual(config["model"], "ollama/qwen2.5-coder:14b")
        self.assertEqual(config["provider"]["ollama"]["options"]["baseURL"], "http://127.0.0.1:11434/v1")
        self.assertIn("qwen2.5-coder:14b", config["provider"]["ollama"]["models"])

    def test_agent_error_summary_reads_opencode_stdout_error(self):
        stdout = '{"type":"error","error":{"name":"APIError","data":{"message":"gemma3:4b does not support tools"}}}\n'

        self.assertEqual(_agent_error_summary(stdout, ""), "gemma3:4b does not support tools")


class CommandCodeBuildCmdTest(unittest.TestCase):
    def tearDown(self):
        brain.fork_thread("cc-thread")

    def test_headless_json_flags(self):
        cmd = _build_cmd("/opt/homebrew/bin/command-code", "cc-thread", "hello")

        self.assertEqual(cmd[0], "/opt/homebrew/bin/command-code")
        self.assertIn("-p", cmd)
        self.assertIn("--yolo", cmd)
        self.assertIn("--skip-onboarding", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        self.assertNotIn("--resume", cmd)
        # system prompt + user message travel as the final positional argument
        self.assertIn("User message from Slack", cmd[-1])

    def test_cmd_alias_is_recognized(self):
        cmd = _build_cmd("/opt/homebrew/bin/cmd", "cc-thread", "hello")

        self.assertIn("--output-format", cmd)
        self.assertNotIn("--append-system-prompt", cmd)

    def test_resumes_existing_session(self):
        brain.set_session_for_thread("cc-thread", "sess_abc")

        cmd = _build_cmd("/opt/homebrew/bin/command-code", "cc-thread", "hello")

        self.assertEqual(cmd[cmd.index("--resume") + 1], "sess_abc")

    def test_defaults_to_deepseek_v4_1_flash(self):
        env = {k: v for k, v in os.environ.items() if k != "COMMAND_CODE_MODEL"}
        with patch.dict(os.environ, env, clear=True):
            cmd = _build_cmd("/opt/homebrew/bin/command-code", "cc-thread", "hello")

        self.assertEqual(cmd[cmd.index("--model") + 1], "deepseek/deepseek-v4.1-flash")

    def test_model_can_be_overridden(self):
        with patch.dict(os.environ, {"COMMAND_CODE_MODEL": "z-ai/glm-5.3-flash"}):
            cmd = _build_cmd("/opt/homebrew/bin/command-code", "cc-thread", "hello")

        self.assertEqual(cmd[cmd.index("--model") + 1], "z-ai/glm-5.3-flash")


class CommandCodeParseTest(unittest.TestCase):
    def test_parses_event_stream_and_result(self):
        stdout = "\n".join(
            [
                '{"type":"event","seq":1,"event":{"type":"tool_queued","toolCallId":"c1","toolName":"read_file","input":{"file_path":"/x"}}}',
                '{"type":"event","seq":2,"event":{"type":"tool_running","toolCallId":"c1","toolName":"read_file","description":"Reading /x"}}',
                '{"type":"event","seq":3,"event":{"type":"tool_completed","toolCallId":"c1","toolName":"read_file","result":"ok"}}',
                '{"type":"result","subtype":"success","sessionId":"sess_1","stopReason":"end_turn","usage":{"inputTokens":1},"durationMs":4200,"finalText":"All done"}',
            ]
        )

        state = _parse_output(stdout, duration_ms=0)

        self.assertEqual(state["reply"], "All done")
        self.assertEqual(state["session_id"], "sess_1")
        self.assertEqual(state["duration_ms"], 4200)
        self.assertEqual(len(state["tool_events"]), 1)
        self.assertEqual(state["tool_events"][0]["status"], "done")
        self.assertEqual(state["tool_events"][0]["input"], {"file_path": "/x"})
        self.assertEqual(state["tool_breadcrumbs"][0]["name"], "read_file")

    def test_tool_error_marks_event(self):
        stdout = "\n".join(
            [
                '{"type":"event","event":{"type":"tool_running","toolCallId":"c9","toolName":"shell_command"}}',
                '{"type":"event","event":{"type":"tool_errored","toolCallId":"c9","toolName":"shell_command","error":"boom"}}',
                '{"type":"result","subtype":"success","sessionId":"s","durationMs":5,"finalText":"done"}',
            ]
        )

        state = _parse_output(stdout, duration_ms=0)

        self.assertEqual(state["tool_events"][0]["status"], "error")
        self.assertEqual(state["tool_events"][0]["name"], "shell_command")

    def test_text_delta_builds_partial_text(self):
        stdout = "\n".join(
            [
                '{"type":"event","event":{"type":"text_delta","delta":"Hello "}}',
                '{"type":"event","event":{"type":"text_delta","delta":"world"}}',
            ]
        )

        state = _parse_output(stdout, duration_ms=0)

        self.assertEqual(state["partial_text"], "Hello world")
        self.assertEqual(state["reply"], "Hello world")


if __name__ == "__main__":
    unittest.main()
