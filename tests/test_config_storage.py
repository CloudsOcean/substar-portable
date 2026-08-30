from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from substar_core import config, credential_store, security
from substar_core.credential_store import (
    ALIGN_DEEPSEEK,
    ASR_GENERIC,
    ASR_QWEN,
    resolve_model_provider_credential,
)


class ConfigStorageTests(unittest.TestCase):
    def test_non_v2_credential_envelope_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "不支持的凭据信封格式"):
                security.unprotect_text(
                    "machine-bound-old-envelope",
                    key_path=Path(temp_dir) / "credentials.key",
                )

    def test_provider_credentials_never_borrow_another_provider_key(self) -> None:
        values = {
            "model_provider:deepseek": "deepseek-key-123",
            "model_provider:glm": "glm-key-123456",
        }
        self.assertEqual(
            resolve_model_provider_credential(values, "glm"), "glm-key-123456"
        )
        self.assertEqual(
            resolve_model_provider_credential(values, "deepseek"),
            "deepseek-key-123",
        )
        self.assertEqual(resolve_model_provider_credential(values, "openai"), "")

    def test_old_unscoped_provider_keys_are_not_accepted(self) -> None:
        values = {"translate_deepseek": "old-deepseek-key"}
        self.assertEqual(resolve_model_provider_credential(values, "deepseek"), "")

    def test_general_editor_defaults_are_part_of_the_persisted_contract(self) -> None:
        self.assertEqual(config.DEFAULTS["shortcut_undo"], "Ctrl+Z")
        self.assertEqual(config.DEFAULTS["shortcut_redo"], "Ctrl+Y")
        self.assertNotIn("default_export_dir", config.DEFAULTS)
        self.assertEqual(config._canonical_shortcut("ctrl+shift+z"), "Ctrl+Shift+Z")
        self.assertIsNone(config._canonical_shortcut("Z"))

    def test_portable_data_dir_is_inside_install_root(self) -> None:
        expected_parent = (config.INSTALL_ROOT / "data").resolve()
        self.assertEqual(config.PORTABLE_APP_DATA_DIR.parent, expected_parent)

    def test_unified_credentials_round_trip_keeps_provider_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active = Path(temp_dir) / "credentials.dpapi"
            wrap = lambda value, **_kwargs: f"wrapped:{value}"
            unwrap = lambda value, **_kwargs: value[len("wrapped:") :]
            values = {
                ASR_QWEN: "qwen-key-123",
                ASR_GENERIC: "qwen-key-123",
                ALIGN_DEEPSEEK: "alignment-key-123",
                "model_provider:deepseek": "llm-key-123",
            }
            with patch.object(config, "CREDENTIALS_FILE", active), patch.object(
                config, "credential_file_candidates", return_value=(active,)
            ), patch.object(credential_store, "protect_text", side_effect=wrap), patch.object(
                credential_store, "unprotect_text", side_effect=unwrap
            ):
                config._write_credential_envelope(values)
                self.assertEqual(config.load_credentials(), values)

            self.assertTrue(active.is_file())

    def test_portable_credential_envelope_survives_directory_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "machine-a"
            destination = Path(temp_dir) / "machine-b"
            source.mkdir()
            destination.mkdir()
            envelope = source / "credentials.enc"
            values = {ASR_QWEN: "portable-qwen-key", "model_provider:deepseek": "portable-llm-key"}

            credential_store.write_envelope(envelope, values)
            (destination / envelope.name).write_bytes(envelope.read_bytes())
            key = source / "credentials.key"
            (destination / key.name).write_bytes(key.read_bytes())

            loaded = credential_store.load_store((destination / envelope.name,))
            self.assertEqual(loaded, values)
            self.assertNotIn("portable-qwen-key", envelope.read_text(encoding="ascii"))

    def test_explicit_translation_clear_removes_canonical_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active = Path(temp_dir) / "credentials.dpapi"
            values = {
                ASR_QWEN: "qwen-key-123",
                ASR_GENERIC: "qwen-key-123",
                ALIGN_DEEPSEEK: "llm-key-123",
                "model_provider:deepseek": "llm-key-123",
            }
            with patch.object(config, "CREDENTIALS_FILE", active), patch.object(
                config, "credential_file_candidates", return_value=(active,)
            ), patch.object(
                credential_store, "protect_text", side_effect=lambda value, **_kwargs: f"wrapped:{value}"
            ), patch.object(
                credential_store,
                "unprotect_text",
                side_effect=lambda value, **_kwargs: value[len("wrapped:") :],
            ):
                config._write_credential_envelope(values)
                saved = config.save_credentials_from_settings(
                    {
                        "active_model_provider": "deepseek",
                        "clear_translation_api_key": True,
                    }
                )
                loaded = config.load_credentials()

            self.assertNotIn("model_provider:deepseek", saved)
            self.assertNotIn("model_provider:deepseek", loaded)

    def test_settings_status_is_derived_from_unified_credentials(self) -> None:
        credentials = {
            ASR_QWEN: "qwen-key-123",
            ASR_GENERIC: "qwen-key-123",
            ALIGN_DEEPSEEK: "llm-key-123",
            "model_provider:deepseek": "llm-key-123",
        }
        # Isolate the provider status contract from local application state.
        with tempfile.TemporaryDirectory() as temp_dir:
            empty_settings = Path(temp_dir) / "settings.json"
            with patch.object(
                config, "load_credentials", return_value=credentials
            ), patch.object(
                config, "SETTINGS_FILE", empty_settings
            ), patch.object(
                config, "_unique_paths", return_value=(empty_settings,)
            ):
                settings = config.load_settings()

        self.assertTrue(settings["api_key_set"])
        self.assertTrue(settings["alignment_api_key_set"])
        self.assertTrue(settings["translation_api_key_set"])


if __name__ == "__main__":
    unittest.main()
