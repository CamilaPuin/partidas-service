import ast
from pathlib import Path

from app.application.errors import CasesServiceError, ServiceIntegrationError
from app.core.service_clients import (
    CasesServiceError as CoreCasesServiceError,
    ServiceIntegrationError as CoreServiceIntegrationError,
)


ROOT = Path(__file__).resolve().parents[1]


def imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def test_domain_and_application_do_not_import_frameworks_or_adapters():
    protected_roots = (
        "fastapi",
        "pydantic",
        "redis",
        "app.core",
        "app.infrastructure",
        "app.repositories",
        "app.routers",
        "app.schemas",
    )
    files = list((ROOT / "app" / "domain").glob("*.py"))
    files.extend((ROOT / "app" / "application").glob("*.py"))

    violations = {
        f"{path.relative_to(ROOT)} imports {module}"
        for path in files
        for module in imported_modules(path)
        if module.startswith(protected_roots)
    }

    assert not violations


def test_service_client_exception_names_remain_backward_compatible():
    assert CoreCasesServiceError is CasesServiceError
    assert CoreServiceIntegrationError is ServiceIntegrationError
