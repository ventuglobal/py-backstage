from pathlib import Path

import pytest

from conftest import BASE, CrearCatalogo, CrearRepo, git
from py_backstage.catalogo import ErrorCatalogo, cargar, relaciones
from py_backstage.modelo import Ref

DESCRIPTOR_FACTURADOR = """\
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: facturador
  description: Emite facturas
spec:
  type: service
  lifecycle: production
  owner: plataforma
  system: tienda
  providesApis: [facturas-http]
  consumesApis: [pedidos-sql]
"""


def test_carga_carpeta(crear_catalogo: CrearCatalogo) -> None:
    base = crear_catalogo("repos: []\n", {"todo.yaml": BASE})
    catalogo = cargar(base, leer_repos=False)
    assert len(catalogo.entidades) == 6
    assert catalogo.problemas == []
    web = catalogo.entidades[Ref("component", "default", "tienda-web")]
    assert catalogo.refs(web, "owner") == [Ref("group", "default", "plataforma")]
    assert ("providesApis", Ref("api", "default", "pedidos-sql")) in relaciones(catalogo)[web.ref]


def test_variable_de_entorno(
    crear_catalogo: CrearCatalogo, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = crear_catalogo("repos: []\n", {"todo.yaml": BASE})
    monkeypatch.setenv("PY_BACKSTAGE_CATALOGO", str(base))
    assert len(cargar(leer_repos=False).entidades) == 6


def test_errores_de_carga(crear_catalogo: CrearCatalogo, tmp_path: Path) -> None:
    with pytest.raises(ErrorCatalogo):
        cargar(tmp_path / "no-existe")
    with pytest.raises(ErrorCatalogo):
        cargar(None)
    base = crear_catalogo("desconocida: 1\n", {"todo.yaml": BASE})
    with pytest.raises(ErrorCatalogo, match="desconocida"):
        cargar(base)


def test_duplicados_y_referencias_rotas(crear_catalogo: CrearCatalogo) -> None:
    roto = """\
apiVersion: backstage.io/v1alpha1
kind: Component
metadata: {name: suelto, description: x}
spec: {type: service, lifecycle: production, owner: nadie, system: tienda,
       consumesApis: [no-existe]}
"""
    base = crear_catalogo(
        "repos: []\n", {"a.yaml": BASE, "b.yaml": BASE.split("---")[0], "c.yaml": roto}
    )
    catalogo = cargar(base, leer_repos=False)
    mensajes = [str(p) for p in catalogo.problemas]
    assert any("duplicada" in m and "group:plataforma" in m for m in mensajes)
    assert any("group:nadie" in m for m in mensajes)
    assert any("api:no-existe" in m for m in mensajes)


def test_lee_descriptor_del_repo_en_origin(
    crear_catalogo: CrearCatalogo, crear_repo: CrearRepo
) -> None:
    repo = crear_repo("facturador", {"catalog-info.yaml": DESCRIPTOR_FACTURADOR})
    # Un cambio local sin empujar no debe verse: se lee origin/main.
    (repo / "catalog-info.yaml").write_text("roto: [", encoding="utf-8")
    base = crear_catalogo("repos:\n  - slug: ejemplo/facturador\n", {"todo.yaml": BASE})
    catalogo = cargar(base)
    facturador = catalogo.entidades[Ref("component", "default", "facturador")]
    assert facturador.anotacion("github.com/project-slug") == "ejemplo/facturador"
    assert facturador.origen == "ejemplo/facturador:catalog-info.yaml"
    assert catalogo.leidos["ejemplo/facturador"][0] == "origin/main"


def test_rama_declarada_y_sin_clon(crear_catalogo: CrearCatalogo, crear_repo: CrearRepo) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"}, rama="development")
    base = crear_catalogo(
        """\
repos:
  - slug: ejemplo/tienda
    rama: development
  - slug: ejemplo/fantasma
""",
        {"todo.yaml": BASE},
    )
    catalogo = cargar(base)
    assert catalogo.leidos["ejemplo/tienda"][0] == "origin/development"
    assert "sin clon local" in catalogo.no_leidos["ejemplo/fantasma"]


def test_fuente_git(crear_repo: CrearRepo, clones: Path) -> None:
    repo = crear_repo(
        "privado",
        {
            "docs/catalogo/py-backstage.yaml": f"raiz_clones: {clones}\nrepos: []\n",
            "docs/catalogo/todo.yaml": BASE,
        },
    )
    git(repo, "rm", "-q", "docs/catalogo/todo.yaml")  # el árbol local no cuenta
    catalogo = cargar(f"git:{repo}@origin/main:docs/catalogo", leer_repos=False)
    assert len(catalogo.entidades) == 6
    with pytest.raises(ErrorCatalogo):
        cargar(f"git:{repo}@origin/main:no/existe", leer_repos=False)


@pytest.mark.parametrize("slug", ["ejemplo/..", "../tienda", "ejemplo/."])
def test_slug_con_puntos_rechazado(crear_catalogo: CrearCatalogo, slug: str) -> None:
    base = crear_catalogo(f"repos:\n  - slug: {slug}\n", {"t.yaml": BASE})
    with pytest.raises(ErrorCatalogo, match="slug inválido"):
        cargar(base, leer_repos=False)


def test_slug_repetido(crear_catalogo: CrearCatalogo) -> None:
    config = "repos:\n  - slug: ejemplo/tienda\n  - slug: Ejemplo/Tienda\n"
    base = crear_catalogo(config, {"t.yaml": BASE})
    with pytest.raises(ErrorCatalogo, match="más de una vez"):
        cargar(base, leer_repos=False)


def test_fuente_git_con_arrobas(crear_repo: CrearRepo, clones: Path) -> None:
    archivos = {
        "docs/catalogo/py-backstage.yaml": f"raiz_clones: {clones}\nrepos: []\n",
        "docs/catalogo/todo.yaml": BASE,
    }
    repo = crear_repo("con@arroba", archivos)
    catalogo = cargar(f"git:{repo}@origin/main:docs/catalogo", leer_repos=False)
    assert len(catalogo.entidades) == 6
    catalogo = cargar(f"git:{repo}@HEAD@{{0}}:docs/catalogo", leer_repos=False)
    assert len(catalogo.entidades) == 6
