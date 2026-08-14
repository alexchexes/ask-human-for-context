"""Tests for contributor development setup documentation and config."""

import os
import re

ROOT_DIR = os.path.join(os.path.dirname(__file__), "..")
EXPECTED_RUNTIME_REQUIREMENTS = [
    "httpx>=0.28.1,<0.29",
    "markdown-it-py>=4.0.0,<5",
    "mcp>=1.23.0,<2",
    "python-multipart>=0.0.27,<0.1",
    "starlette>=1.0.1,<2",
    "uvicorn>=0.49.0,<0.50",
]
EXPECTED_DEV_REQUIREMENTS = [
    "build>=1.5.0,<2",
    "pytest>=9.0.0,<10",
    "black>=26.5.1,<27",
    "isort>=8.0.1,<9",
    "mypy>=2.1.0,<3",
    "pyright>=1.1.405,<2",
    "twine>=6.2.0,<7",
]


def _extract_array_entries(
    pyproject_text: str,
    section_name: str,
    key_name: str,
) -> list[str]:
    """Extract string entries from one array within a TOML section."""
    in_section = False
    collecting = False
    entries: list[str] = []

    for raw_line in pyproject_text.splitlines():
        line = raw_line.strip()

        if line.startswith("[") and line.endswith("]"):
            in_section = line == section_name
            collecting = False
            continue

        if not in_section:
            continue

        if not collecting and line == f"{key_name} = [":
            collecting = True
            continue

        if collecting:
            if line == "]":
                break

            match = re.match(r'"([^"]+)"', line)
            if match:
                entries.append(match.group(1))

    return entries


def _package_names(entries: list[str]) -> set[str]:
    """Extract package names from simple requirement strings."""
    return {re.split(r"[<>=!~ ]", entry, maxsplit=1)[0] for entry in entries}


def test_dev_setup_tooling_is_consistent():
    """Keep uv dev groups aligned with the published dev extra."""
    pyproject_path = os.path.join(ROOT_DIR, "pyproject.toml")
    with open(pyproject_path, encoding="utf-8") as pyproject_file:
        pyproject_text = pyproject_file.read()

    extra_dev = _extract_array_entries(
        pyproject_text,
        "[project.optional-dependencies]",
        "dev",
    )
    group_dev = _extract_array_entries(pyproject_text, "[dependency-groups]", "dev")

    assert extra_dev == EXPECTED_DEV_REQUIREMENTS
    assert group_dev == EXPECTED_DEV_REQUIREMENTS
    assert _package_names(extra_dev) == {
        "black",
        "build",
        "isort",
        "mypy",
        "pyright",
        "pytest",
        "twine",
    }
    assert _package_names(group_dev) == _package_names(extra_dev)


def test_dependency_version_policy_is_explicit():
    """Require reviewed compatibility bounds for dependencies and build tools."""
    pyproject_path = os.path.join(ROOT_DIR, "pyproject.toml")
    with open(pyproject_path, encoding="utf-8") as pyproject_file:
        pyproject_text = pyproject_file.read()

    assert _extract_array_entries(pyproject_text, "[build-system]", "requires") == [
        "hatchling==1.31.0"
    ]
    assert (
        _extract_array_entries(pyproject_text, "[project]", "dependencies")
        == EXPECTED_RUNTIME_REQUIREMENTS
    )
    assert 'requires-python = ">=3.10,<4"' in pyproject_text
    assert 'required-version = ">=0.11.25,<0.12"' in pyproject_text


def test_readme_documents_locked_dev_environment():
    """Document the locked contributor install and check commands."""
    readme_path = os.path.join(ROOT_DIR, "README.md")
    with open(readme_path, encoding="utf-8") as readme_file:
        readme_text = readme_file.read()

    assert "uv sync --locked --all-extras" in readme_text
    assert "uv run --locked pytest" in readme_text


def test_workflows_use_locked_dependencies_and_bounded_actions():
    """Prevent CI and publishing from silently resolving a new dependency graph."""
    workflow_paths = [
        os.path.join(ROOT_DIR, ".github", "workflows", "ci.yml"),
        os.path.join(ROOT_DIR, ".github", "workflows", "publish-pypi.yml"),
    ]
    workflow_texts = []
    for workflow_path in workflow_paths:
        with open(workflow_path, encoding="utf-8") as workflow_file:
            workflow_texts.append(workflow_file.read())

    combined_workflows = "\n".join(workflow_texts)
    assert "pip install" not in combined_workflows
    assert combined_workflows.count("uv sync --locked --all-extras") == 3
    assert combined_workflows.count('version: "0.11.25"') == 3

    action_references = re.findall(
        r"^\s*uses:\s*[^@\s]+@([^\s#]+)", combined_workflows, re.MULTILINE
    )
    assert action_references
    assert all(
        re.fullmatch(r"(?:v\d+|release/v\d+|[0-9a-f]{40})", reference)
        for reference in action_references
    )


def test_console_script_uses_package_name():
    """Expose the package-matching CLI and avoid the old executable name."""
    pyproject_path = os.path.join(ROOT_DIR, "pyproject.toml")
    with open(pyproject_path, encoding="utf-8") as pyproject_file:
        pyproject_text = pyproject_file.read()

    assert 'ask-human = "ask_human:main"' in pyproject_text
    assert not re.search(r"^ask-human-now\s*=", pyproject_text, re.MULTILINE)
