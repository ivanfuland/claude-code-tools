"""Static ownership checks for the public msg communication layer."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MSG_ROOT = ROOT / "claude_code_tools/msg"
PUBLIC_DOCS = (
    ROOT / "docs-site/src/content/docs/tools/msg.mdx",
    ROOT / "release-notes/msg-first-mate-machine-contract.md",
)


def _python_sources() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(MSG_ROOT.glob("*.py"))
    }


def test_first_mate_protocol_literal_is_only_a_schema_migration_value():
    occurrences: list[tuple[str, str]] = []
    for filename, source in _python_sources().items():
        tree = ast.parse(source, filename=filename)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "first-mate.v1" in node.value
            ):
                occurrences.append((filename, node.value))

    assert occurrences == [
        (
            "migrations.py",
            "UPDATE agents SET delivery_mode = 'pull' "
            "WHERE consumer_protocol = 'first-mate.v1'",
        ),
    ]


def test_store_and_models_do_not_own_upper_layer_or_host_title_semantics():
    forbidden = (
        "Mission",
        "controller",
        "executor",
        "candidate",
        "thread/name/set",
        "sessionTitle",
        "/rename",
        "host_session_title",
    )
    for filename in ("models.py", "store.py"):
        source = (MSG_ROOT / filename).read_text(encoding="utf-8")
        assert not {term for term in forbidden if term in source}


def test_candidate_is_only_the_deprecated_cli_option_literal():
    source = (MSG_ROOT / "cli.py").read_text(encoding="utf-8")
    assert source.count("candidate") == 1
    assert '"--replace-candidate"' in source


def test_removed_host_title_and_candidate_contract_are_absent_from_public_docs():
    forbidden = (
        "host_session_title",
        "host session title",
        "sessionTitle",
        "thread/name/set",
        "--replace-candidate",
        "replace-candidate",
    )
    for path in PUBLIC_DOCS:
        source = path.read_text(encoding="utf-8")
        assert not {term for term in forbidden if term in source}, path
