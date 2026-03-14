"""Meta-reflection test ensuring SQLAlchemy and Django backends have matching test coverage.

Uses ast introspection to collect test file names, class names, and method names
from both backends, then asserts they are identical sets. This catches parity
drift automatically when a developer adds a test to one backend but not the other.
"""

import ast
import os
from pathlib import Path


SA_DIR = Path(__file__).parent / "backends" / "sqlalchemy"
DJ_DIR = Path(__file__).parent / "backends" / "django"

EXCLUDED_FILES = {
    "test_query_session_resolution.py",
    "test_query_queryset_detection.py",
}

EXCLUDED_METHODS = {
    ("test_query_error_handling.py", "TestQueryErrorHandling", "test_missing_session_raises_runtime_error"),
}


def _collect_test_structure(directory: Path) -> dict[str, dict[str, list[str]]]:
    """Parse all test_*.py files in *directory* and build a structure of
    {filename: {ClassName: [method_names]}} using the ast module."""
    result: dict[str, dict[str, list[str]]] = {}

    for filepath in sorted(directory.glob("test_*.py")):
        if filepath.name in EXCLUDED_FILES:
            continue

        tree = ast.parse(filepath.read_text(), filename=str(filepath))
        classes: dict[str, list[str]] = {}

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = [
                    n.name
                    for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name.startswith("test_")
                ]
                if methods:
                    classes[node.name] = sorted(methods)

        if classes:
            result[filepath.name] = classes

    return result


class TestBackendParity:
    def test_same_test_files(self):
        sa_struct = _collect_test_structure(SA_DIR)
        dj_struct = _collect_test_structure(DJ_DIR)

        sa_files = set(sa_struct.keys())
        dj_files = set(dj_struct.keys())

        sa_only = sa_files - dj_files
        dj_only = dj_files - sa_files

        assert not sa_only and not dj_only, (
            f"Test file mismatch!\n"
            f"  SA only: {sorted(sa_only) or 'none'}\n"
            f"  Django only: {sorted(dj_only) or 'none'}"
        )

    def test_same_test_classes(self):
        sa_struct = _collect_test_structure(SA_DIR)
        dj_struct = _collect_test_structure(DJ_DIR)

        common_files = set(sa_struct.keys()) & set(dj_struct.keys())
        mismatches = []

        for fname in sorted(common_files):
            sa_classes = set(sa_struct[fname].keys())
            dj_classes = set(dj_struct[fname].keys())
            sa_only = sa_classes - dj_classes
            dj_only = dj_classes - sa_classes
            if sa_only or dj_only:
                mismatches.append(
                    f"  {fname}:\n"
                    f"    SA only classes: {sorted(sa_only) or 'none'}\n"
                    f"    Django only classes: {sorted(dj_only) or 'none'}"
                )

        assert not mismatches, (
            "Test class mismatch between backends!\n" + "\n".join(mismatches)
        )

    def test_same_test_methods(self):
        sa_struct = _collect_test_structure(SA_DIR)
        dj_struct = _collect_test_structure(DJ_DIR)

        common_files = set(sa_struct.keys()) & set(dj_struct.keys())
        mismatches = []

        for fname in sorted(common_files):
            sa_classes = sa_struct[fname]
            dj_classes = dj_struct[fname]
            common_classes = set(sa_classes.keys()) & set(dj_classes.keys())

            for cls_name in sorted(common_classes):
                sa_methods = set(sa_classes[cls_name])
                dj_methods = set(dj_classes[cls_name])

                for m in list(sa_methods | dj_methods):
                    if (fname, cls_name, m) in EXCLUDED_METHODS:
                        sa_methods.discard(m)
                        dj_methods.discard(m)

                sa_only = sa_methods - dj_methods
                dj_only = dj_methods - sa_methods
                if sa_only or dj_only:
                    mismatches.append(
                        f"  {fname}::{cls_name}:\n"
                        f"    SA only: {sorted(sa_only) or 'none'}\n"
                        f"    Django only: {sorted(dj_only) or 'none'}"
                    )

        assert not mismatches, (
            "Test method mismatch between backends!\n" + "\n".join(mismatches)
        )
