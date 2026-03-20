"""Meta-reflection test ensuring all backends have matching test coverage.

Uses ast introspection to collect test file names, class names, and method names
from all backends, then asserts they are identical sets. This catches parity
drift automatically when a developer adds a test to one backend but not the other.
"""

import ast
from pathlib import Path

SA_DIR = Path(__file__).parent / "backends" / "sqlalchemy"
DJ_DIR = Path(__file__).parent / "backends" / "django"
TORT_DIR = Path(__file__).parent / "backends" / "tortoise"

EXCLUDED_FILES = {
    "test_query_session_resolution.py",
    "test_query_queryset_detection.py",
    "test_queryset_detection.py",
    "test_query_async_session.py",
    "test_internal_sqlalchemy_extra_coverage.py",
}

EXCLUDED_METHODS = {
    (
        "test_query_error_handling.py",
        "TestQueryErrorHandling",
        "test_missing_session_raises_runtime_error",
    ),
}


TORT_EXCLUDED_FILES = {
    "test_query_session_resolution.py",
    "test_query_queryset_detection.py",
    "test_query_async_session.py",
    "test_query_async_optimizer_path.py",
    "test_query_one_to_one_runtime.py",
    "test_mutation_node_graph.py",
    "test_query_e2e_extra.py",
    "test_query_basic.py",
    "test_query_filter_field_lookups.py",
    "test_query_filter_boolean_operators.py",
    "test_query_filter_nested_conditions.py",
    "test_query_filter_relationships.py",
    "test_query_filter_object_traversal.py",
    "test_query_filter_object_project.py",
    "test_query_order_direction_and_nulls.py",
    "test_query_order_object_traversal.py",
    "test_query_auto_resolution.py",
    "test_query_type_generation.py",
    "test_query_nested_resolution.py",
    "test_query_self_is_model.py",
    "test_query_multiple_types.py",
    "test_query_get_queryset.py",
    "test_query_error_handling.py",
    "test_mutation_crud.py",
    "test_query_field_hints.py",
    "test_mutation_ref_list.py",
    "test_query_optimizer.py",
    "test_backend.py",
    "test_ref_type.py",
    "test_internal_tortoise_extra_coverage.py",
}


def _collect_test_structure(directory: Path) -> dict[str, dict[str, list[str]]]:
    """Parse all test_*.py files in *directory* and build a structure of
    {filename: {ClassName: [method_names]}} using the ast module."""
    result: dict[str, dict[str, list[str]]] = {}

    for filepath in sorted(directory.glob("test_*.py")):
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


def _filter_structure(
    struct: dict[str, dict[str, list[str]]],
    excluded_files: set[str],
    excluded_methods: set[tuple[str, str, str]] | None = None,
) -> dict[str, dict[str, list[str]]]:
    """Remove excluded files and methods from a test structure."""
    result: dict[str, dict[str, list[str]]] = {}
    for fname, classes in struct.items():
        if fname in excluded_files:
            continue
        filtered_classes: dict[str, list[str]] = {}
        for cls_name, methods in classes.items():
            filtered = [
                m
                for m in methods
                if not excluded_methods or (fname, cls_name, m) not in excluded_methods
            ]
            if filtered:
                filtered_classes[cls_name] = filtered
        if filtered_classes:
            result[fname] = filtered_classes
    return result


class TestBackendParity:
    def test_sa_django_same_test_files(self):
        sa_struct = _filter_structure(_collect_test_structure(SA_DIR), EXCLUDED_FILES)
        dj_struct = _filter_structure(_collect_test_structure(DJ_DIR), EXCLUDED_FILES)

        sa_files = set(sa_struct.keys())
        dj_files = set(dj_struct.keys())

        sa_only = sa_files - dj_files
        dj_only = dj_files - sa_files

        assert not sa_only and not dj_only, (
            f"Test file mismatch!\n"
            f"  SA only: {sorted(sa_only) or 'none'}\n"
            f"  Django only: {sorted(dj_only) or 'none'}"
        )

    def test_sa_django_same_test_classes(self):
        sa_struct = _filter_structure(_collect_test_structure(SA_DIR), EXCLUDED_FILES)
        dj_struct = _filter_structure(_collect_test_structure(DJ_DIR), EXCLUDED_FILES)

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

        assert not mismatches, "Test class mismatch between backends!\n" + "\n".join(
            mismatches
        )

    def test_sa_django_same_test_methods(self):
        sa_struct = _filter_structure(
            _collect_test_structure(SA_DIR), EXCLUDED_FILES, EXCLUDED_METHODS
        )
        dj_struct = _filter_structure(
            _collect_test_structure(DJ_DIR), EXCLUDED_FILES, EXCLUDED_METHODS
        )

        common_files = set(sa_struct.keys()) & set(dj_struct.keys())
        mismatches = []

        for fname in sorted(common_files):
            sa_classes = sa_struct[fname]
            dj_classes = dj_struct[fname]
            common_classes = set(sa_classes.keys()) & set(dj_classes.keys())

            for cls_name in sorted(common_classes):
                sa_methods = set(sa_classes[cls_name])
                dj_methods = set(dj_classes[cls_name])
                sa_only = sa_methods - dj_methods
                dj_only = dj_methods - sa_methods
                if sa_only or dj_only:
                    mismatches.append(
                        f"  {fname}::{cls_name}:\n"
                        f"    SA only: {sorted(sa_only) or 'none'}\n"
                        f"    Django only: {sorted(dj_only) or 'none'}"
                    )

        assert not mismatches, "Test method mismatch between backends!\n" + "\n".join(
            mismatches
        )

    def test_tortoise_has_common_tests(self):
        """Verify Tortoise has parity with SA/Django for non-excluded tests."""
        sa_struct = _filter_structure(
            _collect_test_structure(SA_DIR),
            EXCLUDED_FILES | TORT_EXCLUDED_FILES,
        )
        tort_struct = _filter_structure(
            _collect_test_structure(TORT_DIR),
            EXCLUDED_FILES | TORT_EXCLUDED_FILES,
        )

        sa_files = set(sa_struct.keys())
        tort_files = set(tort_struct.keys())
        sa_only = sa_files - tort_files
        tort_only = tort_files - sa_files

        assert not sa_only and not tort_only, (
            f"Test file mismatch (SA vs Tortoise, excluding known gaps)!\n"
            f"  SA only: {sorted(sa_only) or 'none'}\n"
            f"  Tortoise only: {sorted(tort_only) or 'none'}"
        )

        common_files = sa_files & tort_files
        mismatches = []
        for fname in sorted(common_files):
            sa_classes = set(sa_struct[fname].keys())
            tort_classes = set(tort_struct[fname].keys())
            sa_only_cls = sa_classes - tort_classes
            tort_only_cls = tort_classes - sa_classes
            if sa_only_cls or tort_only_cls:
                mismatches.append(
                    f"  {fname}:\n"
                    f"    SA only classes: {sorted(sa_only_cls) or 'none'}\n"
                    f"    Tortoise only classes: {sorted(tort_only_cls) or 'none'}"
                )

        assert not mismatches, "Test class mismatch (SA vs Tortoise)!\n" + "\n".join(
            mismatches
        )
