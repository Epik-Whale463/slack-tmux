import unittest
from pathlib import Path

from telly.app import _extract_upload_markers, _format_upload_failure


class UploadMarkerTest(unittest.TestCase):
    def test_extract_upload_markers(self):
        text, paths = _extract_upload_markers("Here it is\nSLACK_UPLOAD: /tmp/example.png\nDone")

        self.assertEqual(text, "Here it is\nDone")
        self.assertEqual(paths, [Path("/tmp/example.png")])

    def test_extract_attach_file_alias(self):
        text, paths = _extract_upload_markers("ATTACH_FILE: `/tmp/example.pdf`")

        self.assertEqual(text, "")
        self.assertEqual(paths, [Path("/tmp/example.pdf")])

    def test_format_missing_scope_upload_failure(self):
        err = RuntimeError("missing scope")
        err.response = {"error": "missing_scope", "needed": "files:write"}

        text = _format_upload_failure(Path("/tmp/example.png"), err)

        self.assertIn("/tmp/example.png", text)
        self.assertIn("files:write", text)
        self.assertIn("reinstall", text)


if __name__ == "__main__":
    unittest.main()
