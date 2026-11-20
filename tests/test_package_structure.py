import ast
from graphlib import TopologicalSorter
from importlib.util import resolve_name
from pathlib import Path

import webskrap


def test_implementation_imports_are_acyclic():
    root = Path(webskrap.__file__).parent
    modules = {
        ".".join(("webskrap", *path.relative_to(root).with_suffix("").parts)).removesuffix(
            ".__init__"
        ): path
        for path in root.rglob("*.py")
        if path.parent != root
    }
    dependencies: dict[str, set[str]] = {}
    packages: dict[str, set[str]] = {}
    for module, path in modules.items():
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        imported: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                name = resolve_name("." * node.level + (node.module or ""), package)
                imported.add(name)
                imported.update(f"{name}.{alias.name}" for alias in node.names)

        dependencies[module] = (imported & modules.keys()) - {package}
        layer = ".".join(module.split(".")[:2])
        packages.setdefault(layer, set()).update(
            ".".join(target.split(".")[:2])
            for target in dependencies[module]
            if target != layer and not target.startswith(f"{layer}.")
        )

    TopologicalSorter(dependencies).prepare()
    TopologicalSorter(packages).prepare()


def test_package_root_only_contains_public_api_and_shared_errors():
    root = Path(webskrap.__file__).parent
    assert {path.name for path in root.glob("*.py")} == {"__init__.py", "errors.py"}
