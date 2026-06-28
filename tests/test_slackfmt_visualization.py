import unittest

from telly.slackfmt import extract_visualization_blocks


class VisualizationBlocksTest(unittest.TestCase):
    def test_extracts_flat_unfenced_data_visualization(self):
        text = '''NVDA summary.

{
  "type": "data_visualization",
  "chart_type": "line",
  "title": "NVDA Closing Price - Last 3 Months",
  "x_axis": "Date",
  "y_axis": "Close USD",
  "data": [
    {"date": "2026-03-27", "close": 167.52},
    {"date": "2026-04-02", "close": 177.39}
  ]
}
'''

        cleaned, blocks = extract_visualization_blocks(text)

        self.assertEqual(cleaned, "NVDA summary.")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "data_visualization")
        self.assertEqual(blocks[0]["chart"]["type"], "line")
        self.assertEqual(blocks[0]["chart"]["axis_config"]["categories"], ["2026-03-27", "2026-04-02"])
        self.assertEqual(blocks[0]["chart"]["series"][0]["name"], "Close USD")

    def test_extracts_fenced_native_data_visualization(self):
        text = '''Here.
```slack-visualization
{"type":"data_visualization","title":"Weekly","chart":{"type":"line","series":[{"name":"Price","data":[{"label":"W1","value":1}]}],"axis_config":{"categories":["W1"]}}}
```
'''

        cleaned, blocks = extract_visualization_blocks(text)

        self.assertEqual(cleaned, "Here.")
        self.assertEqual(blocks[0]["title"], "Weekly")

    def test_extracts_nested_simplified_chart_payload(self):
        text = '''Here’s a line chart.

{
  "type": "data_visualization",
  "chart": {
    "type": "line",
    "title": "NVDA Stock Price - Last 3 Months",
    "x": "date",
    "y": "close",
    "y_label": "Close Price ($)",
    "data": [
      {"date": "2026-03-25", "close": 178.68},
      {"date": "2026-04-01", "close": 175.75}
    ]
  }
}

Summary text.'''

        cleaned, blocks = extract_visualization_blocks(text)

        self.assertEqual(cleaned, "Here’s a line chart.\n\nSummary text.")
        self.assertEqual(blocks[0]["title"], "NVDA Stock Price - Last 3 Months")
        self.assertEqual(blocks[0]["chart"]["type"], "line")
        self.assertEqual(blocks[0]["chart"]["series"][0]["name"], "Close Price ($)")
        self.assertEqual(blocks[0]["chart"]["axis_config"]["categories"], ["2026-03-25", "2026-04-01"])


if __name__ == "__main__":
    unittest.main()
