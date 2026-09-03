from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gs1_scraper.config import ConfigurationError, load_env_file, load_keywords


class ConfigTests(unittest.TestCase):
    def test_env_file_does_not_override_real_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("GS1_EMAIL=file@example.com\nGS1_PHONE=0500000000\n")
            with patch.dict(os.environ, {"GS1_EMAIL": "actual@example.com"}, clear=True):
                load_env_file(env_path)
                self.assertEqual(os.environ["GS1_EMAIL"], "actual@example.com")
                self.assertEqual(os.environ["GS1_PHONE"], "0500000000")

    def test_keywords_ignore_comments_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            keyword_path = Path(directory) / "keywords.txt"
            keyword_path.write_text("# comment\nיין\nבירה\nיין\n", encoding="utf-8")
            self.assertEqual(load_keywords(keyword_path), ["יין", "בירה"])

    def test_empty_keyword_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            keyword_path = Path(directory) / "keywords.txt"
            keyword_path.write_text("# comment only\n", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_keywords(keyword_path)
