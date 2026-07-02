import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
