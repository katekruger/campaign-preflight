"""Configuration loading and validation.

The behaviour under test is intolerance: a typo in a safety config must be an
error, not a silently ignored line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from campaign_preflight.config import (
    CONFIG_SCHEMA_VERSION,
    PreflightConfig,
    load_config,
    load_config_document,
    safe_resolve,
)
from campaign_preflight.errors import ConfigurationError, InputError


class TestDefaults:
    def test_empty_document_yields_defaults(self) -> None:
        config = load_config_document({})
        assert config.version == CONFIG_SCHEMA_VERSION
        assert config.settings.required_variables == ("first_name",)
        assert config.evidence.evaluator == "disabled"

    def test_none_path_yields_defaults(self) -> None:
        assert load_config(None) == PreflightConfig()

    def test_llm_evaluation_is_off_by_default(self) -> None:
        """No data leaves the machine unless the user explicitly opts in."""
        assert PreflightConfig().evidence.evaluator == "disabled"


class TestValidation:
    def test_unknown_rule_id_is_an_error(self) -> None:
        with pytest.raises(ConfigurationError, match="unknown rule id"):
            load_config_document({"rules": {"contacts.missing_firstname": {"enabled": False}}})

    def test_unknown_rule_id_suggests_a_correction(self) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            load_config_document({"rules": {"contacts.missing_first_nam": {}}})
        assert "contacts.missing_first_name" in str(excinfo.value)

    def test_unknown_option_within_a_rule_is_an_error(self) -> None:
        with pytest.raises(ConfigurationError, match="unknown option"):
            load_config_document({"rules": {"campaign.daily_volume": {"warning_abov": 10}}})

    def test_unknown_option_suggests_a_correction(self) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            load_config_document({"rules": {"campaign.daily_volume": {"warning_abov": 10}}})
        assert "warning_above" in str(excinfo.value)

    def test_wrong_option_type_is_an_error(self) -> None:
        with pytest.raises(ConfigurationError):
            load_config_document({"rules": {"campaign.daily_volume": {"warning_above": "lots"}}})

    def test_out_of_range_option_is_an_error(self) -> None:
        with pytest.raises(ConfigurationError):
            load_config_document({"settings": {"max_samples": 5000}})

    def test_unknown_top_level_key_is_an_error(self) -> None:
        with pytest.raises(ConfigurationError):
            load_config_document({"rulez": {}})

    def test_unknown_setting_is_an_error(self) -> None:
        with pytest.raises(ConfigurationError):
            load_config_document({"settings": {"targt_timezone": "UTC"}})

    def test_unsupported_version_is_an_error(self) -> None:
        with pytest.raises(ConfigurationError, match="unsupported config version"):
            load_config_document({"version": 99})

    def test_non_mapping_document_is_an_error(self) -> None:
        with pytest.raises(ConfigurationError, match="must be a mapping"):
            load_config_document(["not", "a", "mapping"])

    def test_rule_options_must_be_a_mapping(self) -> None:
        with pytest.raises(ConfigurationError, match="expected a mapping of options"):
            load_config_document({"rules": {"campaign.daily_volume": True}})

    def test_valid_config_round_trips(self) -> None:
        config = load_config_document(
            {
                "version": 1,
                "settings": {"target_timezone": "Europe/London", "max_samples": 3},
                "rules": {"campaign.daily_volume": {"warning_above": 50, "blocker_above": 90}},
            }
        )
        assert config.settings.target_timezone == "Europe/London"
        assert config.rules["campaign.daily_volume"]["blocker_above"] == 90


class TestNormalizationOfSettings:
    def test_domain_lists_are_lowercased_and_deduplicated(self) -> None:
        config = load_config_document(
            {"settings": {"internal_domains": ["Example.COM", "example.com", "@example.com"]}}
        )
        assert config.settings.internal_domains == ("example.com",)

    def test_regions_are_uppercased(self) -> None:
        config = load_config_document({"settings": {"restricted_regions": ["de", " fr "]}})
        assert config.settings.restricted_regions == ("DE", "FR")


class TestFileLoading:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(InputError, match="not found"):
            load_config(tmp_path / "nope.yaml")

    def test_malformed_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("rules: [unclosed\n", encoding="utf-8")
        with pytest.raises(InputError, match="not valid YAML"):
            load_config(path)

    def test_oversized_file_is_refused(self, tmp_path: Path) -> None:
        from campaign_preflight.config import MAX_CONFIG_BYTES

        path = tmp_path / "huge.yaml"
        path.write_text("# " + "x" * (MAX_CONFIG_BYTES + 10), encoding="utf-8")
        with pytest.raises(InputError, match="above the"):
            load_config(path)

    def test_bom_is_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "bom.yaml"
        path.write_bytes("﻿version: 1\n".encode())
        assert load_config(path).version == 1

    def test_json_is_accepted_as_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text('{"version": 1, "settings": {"max_samples": 2}}', encoding="utf-8")
        assert load_config(path).settings.max_samples == 2

    def test_yaml_cannot_construct_python_objects(self, tmp_path: Path) -> None:
        """safe_load only: a config file must not be able to execute anything."""
        path = tmp_path / "evil.yaml"
        path.write_text("!!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8")
        with pytest.raises(InputError):
            load_config(path)


class TestSafeResolve:
    def test_symlinks_are_refused(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("CAMPAIGN_PREFLIGHT_ALLOW_SYMLINKS", raising=False)
        real = tmp_path / "real.yaml"
        real.write_text("version: 1\n", encoding="utf-8")
        link = tmp_path / "link.yaml"
        link.symlink_to(real)
        with pytest.raises(InputError, match="symlink"):
            safe_resolve(link)

    def test_symlinks_can_be_opted_into(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CAMPAIGN_PREFLIGHT_ALLOW_SYMLINKS", "1")
        real = tmp_path / "real.yaml"
        real.write_text("version: 1\n", encoding="utf-8")
        link = tmp_path / "link.yaml"
        link.symlink_to(real)
        assert safe_resolve(link) == real.resolve()


def test_example_configs_are_valid(examples_dir: Path) -> None:
    for path in sorted(examples_dir.glob("*/config.yaml")):
        assert load_config(path).version == 1
