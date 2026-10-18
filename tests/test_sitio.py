import subprocess
import sys
from pathlib import Path

import pytest

from conftest import BASE, CrearCatalogo, CrearRepo, componente
from py_backstage.auditoria import auditar
from py_backstage.catalogo import cargar
from py_backstage.modelo import Ref
from py_backstage.sitio import ErrorSitio, _id, _pagina, generar, grafo_mermaid

EXTRA = componente("facturador", "ejemplo/facturador", consumesApis="[pedidos-sql]")
REPOS = "repos:\n  - slug: ejemplo/tienda\n  - slug: ejemplo/facturador\n"


def test_grafo(crear_catalogo: CrearCatalogo) -> None:
    catalogo = cargar(crear_catalogo(REPOS, {"t.yaml": BASE + EXTRA}), leer_repos=False)
    grafo = grafo_mermaid(catalogo)
    assert grafo.startswith("flowchart LR")
    assert "component_ctienda_dweb ==>|provee| api_cpedidos_dsql" in grafo
    assert "component_cfacturador -.->|consume| api_cpedidos_dsql" in grafo
    solo = grafo_mermaid(catalogo, Ref("system", "default", "tienda"))
    assert "subgraph system_ctienda" in solo


def test_ids_y_paginas_no_chocan() -> None:
    refs = [
        Ref("component", "default", "a-b"),
        Ref("component", "default", "a.b"),
        Ref("component", "default", "a_b"),
        Ref("component", "a", "b-c"),
        Ref("component", "a-b", "c"),
    ]
    assert len({_id(r) for r in refs}) == len(refs)
    assert len({_pagina(r) for r in refs}) == len(refs)


def test_sitio_y_mkdocs_estricto(
    crear_catalogo: CrearCatalogo, crear_repo: CrearRepo, tmp_path: Path
) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo("facturador", {"a.py": 'cur.execute("SELECT 1 FROM pedidos_x")\n'})
    catalogo = cargar(crear_catalogo(REPOS, {"t.yaml": BASE + EXTRA}))
    salida = tmp_path / "sitio"
    generar(catalogo, salida, titulo="Catálogo: prueba", resultado=auditar(catalogo))
    docs = salida / "docs"
    assert (docs / "index.md").exists() and (docs / "grafo.md").exists()
    ficha = (docs / "entidades/api/default/pedidos-sql.md").read_text(encoding="utf-8")
    assert "## La usan" in ficha and "facturador" in ficha
    assert "## V15" in (docs / "auditoria.md").read_text(encoding="utf-8")
    mkdocs = [sys.executable, "-m", "mkdocs", "build", "--strict", "-q"]
    subprocess.run(
        [*mkdocs, "-f", str(salida / "mkdocs.yml")],
        check=True,
        capture_output=True,
    )
    assert (salida / "site" / "index.html").exists()
    # Regenerar sobre su propia salida está permitido.
    generar(catalogo, salida)


def test_sitio_no_pisa_carpetas_ajenas(crear_catalogo: CrearCatalogo, tmp_path: Path) -> None:
    catalogo = cargar(crear_catalogo(REPOS, {"t.yaml": BASE}), leer_repos=False)
    ajena = tmp_path / "ajena"
    ajena.mkdir()
    (ajena / "importante.txt").write_text("no borrar", encoding="utf-8")
    with pytest.raises(ErrorSitio):
        generar(catalogo, ajena)
    assert (ajena / "importante.txt").exists()
    archivo = tmp_path / "archivo"
    archivo.write_text("x", encoding="utf-8")
    with pytest.raises(ErrorSitio, match="no es una carpeta"):
        generar(catalogo, archivo)


def test_titulo_con_emoji(crear_catalogo: CrearCatalogo, tmp_path: Path) -> None:
    catalogo = cargar(crear_catalogo(REPOS, {"t.yaml": BASE}), leer_repos=False)
    generar(catalogo, tmp_path / "sitio", titulo='Catálogo 🚀 "uno": #2')
    config = (tmp_path / "sitio" / "mkdocs.yml").read_text(encoding="utf-8")
    assert 'site_name: "Catálogo 🚀 \\"uno\\": #2"' in config


MALICIOSO = """\
---
apiVersion: backstage.io/v1alpha1
kind: API
metadata:
  name: rara
  title: 'Título "raro" con `tilde` y [enlace](javascript:alert(1))'
  description: '<script>alert(1)</script> **negrita** | celda'
  tags: [a-b]
  links:
    - url: javascript:alert(2)
      title: <b>malo</b>
    - url: https://ejemplo.test/a b
      title: bueno
  annotations:
    github.com/project-slug: 'ejemplo/x"><img src=y onerror=alert(3)>'
    py-backstage/ruta: 'a|b`c'
spec:
  type: openapi
  lifecycle: production
  owner: plataforma
  system: tienda
  definition: |
    ```
    </code><script>alert(4)</script>
"""


def test_sitio_escapa_lo_que_viene_de_los_descriptores(
    crear_catalogo: CrearCatalogo, crear_repo: CrearRepo, tmp_path: Path
) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo(
        "facturador",
        {"a.py": 'cur.execute("SELECT 1 FROM pedidos_x")  # <img src=z onerror=alert(5)> | `\n'},
    )
    extra = componente("facturador", "ejemplo/facturador") + MALICIOSO
    catalogo = cargar(crear_catalogo(REPOS, {"t.yaml": BASE + extra}))
    salida = tmp_path / "sitio"
    generar(catalogo, salida, titulo='Catálogo "x": y', resultado=auditar(catalogo))
    mkdocs = [sys.executable, "-m", "mkdocs", "build", "--strict", "-q"]
    subprocess.run([*mkdocs, "-f", str(salida / "mkdocs.yml")], check=True, capture_output=True)

    html = "\n".join(p.read_text(encoding="utf-8") for p in (salida / "site").rglob("*.html"))
    peligrosos = (
        "alert(1)</script>",
        "<img src=",
        'href="javascript:',
        "<b>malo",
        "alert(4)</script>",
    )
    for peligroso in peligrosos:
        assert peligroso not in html, peligroso
    ficha = (salida / "site/entidades/api/default/rara/index.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(1)&lt;/script&gt; **negrita** | celda" in ficha
    assert 'href="https://ejemplo.test/a%20b"' in ficha
    assert "javascript:alert(2)" in ficha  # visible como texto, no como enlace
    auditoria = (salida / "site/auditoria/index.html").read_text(encoding="utf-8")
    assert "&lt;img src=z onerror=alert(5)&gt; | `" in auditoria
