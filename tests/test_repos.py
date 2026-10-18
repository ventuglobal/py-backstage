from pathlib import Path

import pytest

from conftest import CrearRepo, git
from py_backstage.repos import (
    PATRONES_PRUEBAS,
    Archivo,
    Repo,
    candidatos_evidencia,
    coincide,
    es_plantilla_env,
    normalizar_globs,
)


@pytest.mark.parametrize(
    ("patron", "si", "no"),
    [
        ("*.sql", ["a.sql", "db/x/a.sql"], ["a.sql.py"]),
        ("scripts", ["scripts/a.py", "app/scripts/b.py", "scripts"], ["misscripts/a.py"]),
        ("/seed.sql", ["seed.sql"], ["db/seed.sql"]),
        ("./seed.sql", ["seed.sql"], ["db/seed.sql"]),
        ("app/migraciones", ["app/migraciones/0001.py"], ["x/app/migraciones/0001.py"]),
        ("vendor/", ["vendor/a.js", "src/vendor/a.js"], ["vendors/a.js"]),
        ("vendor/**", ["vendor/a.js", "vendor/b/c.js"], ["src/vendor/a.js"]),
        ("**/fixtures", ["fixtures/a.json", "app/fixtures/a.json"], ["fixtures.py"]),
        ("**/gen/*.py", ["gen/a.py", "x/gen/a.py"], ["gen/a.ts"]),
        # ``*`` cruza ``/`` (fnmatch): incluye las subcarpetas.
        ("app/*.py", ["app/a.py", "app/sub/a.py"], ["otra/app/a.py"]),
    ],
)
def test_globs_como_gitignore(patron: str, si: list[str], no: list[str]) -> None:
    globs = normalizar_globs([patron])
    assert [r for r in si if not coincide(r, globs)] == []
    assert [r for r in no if coincide(r, globs)] == []


def test_globs_vacios_y_duplicados() -> None:
    assert normalizar_globs(["", "  ", "/", "./"]) == []
    assert normalizar_globs(["scripts", "scripts/", " scripts "]) == normalizar_globs(["scripts"])


@pytest.mark.parametrize(
    ("ruta", "esperado"),
    [
        ("tests/a.py", True),
        ("app/tests/a.py", True),
        ("test_x.py", True),
        ("app/x_test.go", True),
        ("web/a.spec.ts", True),
        ("app/src/test/java/A.java", True),
        ("conftest.py", True),
        ("contest.py", False),
        ("latest/a.py", False),
        ("attestation.py", False),
    ],
)
def test_patrones_de_pruebas(ruta: str, esperado: bool) -> None:
    assert coincide(ruta, PATRONES_PRUEBAS) is esperado


@pytest.mark.parametrize(
    ("ruta", "esperado"),
    [
        (".env.example", True),
        ("app/.env.sample", True),
        (".env.template", True),
        (".env", False),
        (".env.local", False),
        ("env.example", True),
        ("deploy/config.env.example", True),
        (".env.local.sample", True),
        ("prod_env.template", True),
        ("config.example", False),
        ("settings.dist", False),
        (".env.example.bak", False),
    ],
)
def test_plantillas_env(ruta: str, esperado: bool) -> None:
    assert es_plantilla_env(ruta) is esperado


def test_candidatos_evidencia() -> None:
    rutas = [
        "src/a.py",
        "catalog-info.yaml",
        ".copier-answers.yml",
        "node_modules/x/index.js",
        "tests/test_a.py",
        "docs/uso.md",
        "README.md",
        "imagen.png",
        "generado/a.py",
    ]
    archivos = [Archivo(r, "0" * 40, 10) for r in rutas] + [Archivo("grande.py", "0" * 40, 10**7)]
    elegidos = [a.ruta for a in candidatos_evidencia(archivos, ["generado"])]
    assert elegidos == ["src/a.py"]
    con_todo = candidatos_evidencia(archivos, incluir_pruebas=True, incluir_docs=True)
    assert {a.ruta for a in con_todo} == {
        "src/a.py",
        "tests/test_a.py",
        "docs/uso.md",
        "README.md",
        "generado/a.py",
    }


def test_actualizar_solo_trae_la_rama(crear_repo: CrearRepo, tmp_path: Path) -> None:
    remoto = crear_repo("remoto", {"a.py": "x = 1\n"})
    git(remoto, "branch", "otra")
    clon = tmp_path / "clon"
    git(tmp_path, "clone", "-q", str(remoto), str(clon))
    (remoto / "b.py").write_text("y = 2\n", encoding="utf-8")
    git(remoto, "add", "-A")
    git(remoto, "commit", "-q", "--no-gpg-sign", "-m", "segundo")
    git(remoto, "checkout", "-q", "otra")
    git(remoto, "commit", "-q", "--no-gpg-sign", "--allow-empty", "-m", "en otra")
    antes_otra = git(clon, "rev-parse", "origin/otra")

    repo = Repo("ejemplo/remoto", clon, "main")
    repo.actualizar()
    assert {a.ruta for a in repo.archivos(repo.referencia())} == {"a.py", "b.py"}
    # El refspec explícito no toca las otras ramas remotas.
    assert git(clon, "rev-parse", "origin/otra") == antes_otra
