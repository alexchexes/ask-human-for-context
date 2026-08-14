# Test basic package functionality
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_package_import():
    """Test that the package can be imported successfully."""
    import ask_human

    assert ask_human is not None


def test_package_version():
    """Test that the package version is correct."""
    import ask_human

    assert ask_human.__version__ == "0.3.9"
