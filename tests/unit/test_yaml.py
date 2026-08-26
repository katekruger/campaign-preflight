"""The bundled YAML-subset parser.

Campaign Preflight ships with no third-party dependencies, so this parser
replaces PyYAML. Where PyYAML is installed (development and CI), the
differential tests below assert the two agree exactly -- values and types -- on
every document the tool actually reads.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from campaign_preflight._yaml import YamlError, safe_load

pyyaml = pytest.importorskip("yaml", reason="differential tests need PyYAML")


class TestScalars:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("a: hello", {"a": "hello"}),
            ("a: 42", {"a": 42}),
            ("a: -7", {"a": -7}),
            ("a: 1.5", {"a": 1.5}),
            ("a: 1e3", {"a": 1000.0}),
            ("a: true", {"a": True}),
            ("a: False", {"a": False}),
            ("a: yes", {"a": True}),
            ("a: null", {"a": None}),
            ("a: ~", {"a": None}),
            ("a:", {"a": None}),
            ("a: 'quoted'", {"a": "quoted"}),
            ('a: "quoted"', {"a": "quoted"}),
            ("a: '42'", {"a": "42"}),
            ('a: "line\\nbreak"', {"a": "line\nbreak"}),
            ("a: it's", {"a": "it's"}),
            ("a: 'it''s'", {"a": "it's"}),
        ],
    )
    def test_scalar_forms(self, source: str, expected: dict) -> None:
        assert safe_load(source) == expected

    def test_numeric_strings_stay_strings_when_quoted(self) -> None:
        assert safe_load("a: '09:00'") == {"a": "09:00"}

    def test_time_values_are_not_split_on_the_colon(self) -> None:
        assert safe_load('start: "09:00"\nend: "17:30"') == {"start": "09:00", "end": "17:30"}

    def test_urls_survive_the_colon(self) -> None:
        assert safe_load("url: https://example.com/a") == {"url": "https://example.com/a"}


class TestTimestamps:
    def test_iso_dates_become_date_objects(self) -> None:
        assert safe_load("d: 2026-09-01") == {"d": datetime.date(2026, 9, 1)}

    def test_iso_datetimes_become_datetime_objects(self) -> None:
        parsed = safe_load("d: 2026-09-01T12:30:00Z")["d"]
        assert isinstance(parsed, datetime.datetime)
        assert parsed.tzinfo is not None

    def test_quoted_dates_stay_strings(self) -> None:
        assert safe_load("d: '2026-09-01'") == {"d": "2026-09-01"}

    def test_impossible_dates_stay_strings(self) -> None:
        assert safe_load("d: 2026-13-45") == {"d": "2026-13-45"}


class TestStructures:
    def test_nested_mapping(self) -> None:
        source = "a:\n  b:\n    c: 1\n"
        assert safe_load(source) == {"a": {"b": {"c": 1}}}

    def test_block_sequence(self) -> None:
        assert safe_load("a:\n  - 1\n  - 2\n") == {"a": [1, 2]}

    def test_sequence_not_indented_under_its_key(self) -> None:
        """Valid YAML: a sequence may sit at the same indent as its key."""
        assert safe_load("a:\n- 1\n- 2\n") == {"a": [1, 2]}

    def test_sequence_of_mappings(self) -> None:
        source = "items:\n  - name: a\n    value: 1\n  - name: b\n    value: 2\n"
        assert safe_load(source) == {
            "items": [{"name": "a", "value": 1}, {"name": "b", "value": 2}]
        }

    def test_deeply_nested_sequence_of_mappings(self) -> None:
        source = (
            "sequences:\n"
            "  - steps:\n"
            "      - type: email\n"
            "        variants:\n"
            "          - subject: Hi\n"
            "            body: There\n"
        )
        assert safe_load(source) == {
            "sequences": [
                {"steps": [{"type": "email", "variants": [{"subject": "Hi", "body": "There"}]}]}
            ]
        }

    def test_top_level_sequence(self) -> None:
        assert safe_load("- a\n- b\n") == ["a", "b"]

    def test_flow_sequence(self) -> None:
        assert safe_load("days: [mon, tue, wed]") == {"days": ["mon", "tue", "wed"]}

    def test_flow_sequence_of_numbers(self) -> None:
        assert safe_load("days: [1, 2, 3]") == {"days": [1, 2, 3]}

    def test_empty_flow_sequence(self) -> None:
        assert safe_load("days: []") == {"days": []}

    def test_flow_mapping(self) -> None:
        assert safe_load('timing: {from: "09:00", to: "17:00"}') == {
            "timing": {"from": "09:00", "to": "17:00"}
        }

    def test_flow_sequence_with_quoted_commas(self) -> None:
        assert safe_load('a: ["x, y", z]') == {"a": ["x, y", "z"]}

    def test_empty_document(self) -> None:
        assert safe_load("") is None
        assert safe_load("# only a comment\n") is None


class TestComments:
    def test_full_line_comments_are_ignored(self) -> None:
        assert safe_load("# lead\na: 1  # trailing\n") == {"a": 1}

    def test_hashes_inside_quotes_survive(self) -> None:
        assert safe_load('a: "x # y"') == {"a": "x # y"}

    def test_hash_without_leading_space_is_not_a_comment(self) -> None:
        assert safe_load("a: x#y") == {"a": "x#y"}


class TestBlockScalars:
    def test_literal_block_keeps_newlines(self) -> None:
        source = "body: |\n  line one\n  line two\n"
        assert safe_load(source) == {"body": "line one\nline two\n"}

    def test_literal_strip_chomps_the_trailing_newline(self) -> None:
        source = "body: |-\n  line one\n  line two\n"
        assert safe_load(source) == {"body": "line one\nline two"}

    def test_literal_block_preserves_blank_lines(self) -> None:
        source = "body: |\n  para one\n\n  para two\n"
        assert safe_load(source)["body"] == "para one\n\npara two\n"

    def test_literal_block_preserves_relative_indentation(self) -> None:
        source = "body: |\n  outer\n    inner\n"
        assert safe_load(source)["body"] == "outer\n  inner\n"

    def test_block_scalar_content_is_not_comment_stripped(self) -> None:
        source = "body: |\n  a # not a comment\n"
        assert safe_load(source)["body"] == "a # not a comment\n"

    def test_block_scalar_inside_a_sequence_item(self) -> None:
        source = (
            "steps:\n  - subject: Hi\n    body: |\n      Hello\n      There\n  - subject: Bye\n"
        )
        parsed = safe_load(source)
        assert parsed["steps"][0]["body"] == "Hello\nThere\n"
        assert parsed["steps"][1] == {"subject": "Bye"}

    def test_folded_block_joins_lines(self) -> None:
        source = "body: >\n  one\n  two\n"
        assert safe_load(source)["body"] == "one two\n"


class TestJsonCompatibility:
    def test_json_object(self) -> None:
        assert safe_load('{"a": 1, "b": [1, 2]}') == {"a": 1, "b": [1, 2]}

    def test_json_array(self) -> None:
        assert safe_load('[{"a": 1}]') == [{"a": 1}]

    def test_json_null_and_booleans(self) -> None:
        assert safe_load('{"a": null, "b": true}') == {"a": None, "b": True}


class TestErrors:
    def test_tab_indentation_is_rejected(self) -> None:
        with pytest.raises(YamlError, match="tab used for indentation"):
            safe_load("a:\n\tb: 1\n")

    def test_duplicate_key_is_rejected(self) -> None:
        with pytest.raises(YamlError, match="duplicate key"):
            safe_load("a: 1\na: 2\n")

    def test_anchors_are_rejected_not_ignored(self) -> None:
        with pytest.raises(YamlError, match="anchors and aliases"):
            safe_load("a: &anchor 1\nb: *anchor\n")

    def test_merge_keys_are_rejected(self) -> None:
        with pytest.raises(YamlError, match="merge keys"):
            safe_load("a:\n  <<: *base\n")

    def test_tags_are_rejected(self) -> None:
        with pytest.raises(YamlError, match="tags are not supported"):
            safe_load("a: !!python/object/apply:os.system ['echo pwned']\n")

    def test_multiple_documents_are_rejected(self) -> None:
        with pytest.raises(YamlError, match="multiple documents"):
            safe_load("a: 1\n---\nb: 2\n")

    def test_unterminated_quote_is_rejected(self) -> None:
        with pytest.raises(YamlError, match="unterminated"):
            safe_load('a: "unclosed\n')

    def test_unbalanced_flow_sequence_is_rejected(self) -> None:
        with pytest.raises(YamlError, match="unterminated flow sequence"):
            safe_load("a: [1, 2\n")

    def test_bad_indentation_is_rejected(self) -> None:
        with pytest.raises(YamlError, match="unexpected indentation"):
            safe_load("a: 1\n    b: 2\n")

    def test_missing_colon_is_rejected(self) -> None:
        with pytest.raises(YamlError, match="expected 'key: value'"):
            safe_load("a: 1\njust a bare line\n")

    def test_errors_name_the_line(self) -> None:
        with pytest.raises(YamlError) as excinfo:
            safe_load("a: 1\nb: 2\na: 3\n")
        assert excinfo.value.line_number == 3

    def test_non_string_input_is_rejected(self) -> None:
        with pytest.raises(YamlError, match="expected a string"):
            safe_load(b"a: 1")  # type: ignore[arg-type]


class TestNoCodeExecution:
    def test_a_python_object_tag_cannot_construct_anything(self) -> None:
        """The reason this parser refuses tags rather than skipping them."""
        with pytest.raises(YamlError):
            safe_load("!!python/object/apply:subprocess.check_output [['echo', 'pwned']]\n")


def _repo_documents() -> list[Path]:
    root = Path(__file__).resolve().parents[2]
    return sorted(
        [
            *(root / "src" / "campaign_preflight" / "demo").glob("*.yaml"),
            *(root / "src" / "campaign_preflight" / "demo").glob("*.json"),
            *(root / "examples").glob("*/*.yaml"),
            *(root / "examples").glob("*/*.json"),
        ]
    )


def _same(left: object, right: object) -> bool:
    """Deep equality that also requires matching types."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        assert isinstance(right, dict)
        return set(left) == set(right) and all(_same(left[k], right[k]) for k in left)
    if isinstance(left, list):
        assert isinstance(right, list)
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


@pytest.mark.parametrize("path", _repo_documents(), ids=lambda p: p.parent.name + "/" + p.name)
def test_matches_pyyaml_on_every_shipped_document(path: Path) -> None:
    """Differential test against the reference implementation."""
    source = path.read_text(encoding="utf-8")
    assert _same(pyyaml.safe_load(source), safe_load(source)), (
        f"{path} parses differently from PyYAML"
    )


@pytest.mark.parametrize(
    "source",
    [
        "a: 1\nb: two\nc: [1, 2]\n",
        "a:\n  - x: 1\n    y: 2\n",
        "top:\n  nested:\n    deep: value\n",
        "list:\n  - 1\n  - 2\n  - 3\n",
        'q: "with spaces"\n',
        "n: null\nt: true\nf: false\n",
        "d: 2026-01-01\n",
        "e: []\n",
        "body: |\n  one\n  two\n",
    ],
)
def test_matches_pyyaml_on_representative_snippets(source: str) -> None:
    assert _same(pyyaml.safe_load(source), safe_load(source))


def test_json_round_trip_matches_stdlib() -> None:
    payload = {"a": 1, "b": [1, 2, {"c": None}], "d": True, "e": "x"}
    assert safe_load(json.dumps(payload)) == payload
