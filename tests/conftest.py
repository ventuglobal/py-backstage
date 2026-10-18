"""Repos git y catálogos sintéticos en tmp_path (sin red, sin config global de git)."""

from __future__ import annotations

import os
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

ENTORNO_GIT = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "prueba",
    "GIT_AUTHOR_EMAIL": "prueba@ejemplo.test",
    "GIT_COMMITTER_NAME": "prueba",
    "GIT_COMMITTER_EMAIL": "prueba@ejemplo.test",
}


def git(ruta: Path, *args: str) -> str:
    salida = subprocess.run(
        ["git", "-C", str(ruta), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **ENTORNO_GIT},
    )
    return salida.stdout


def escribir(base: Path, archivos: dict[str, str]) -> None:
    for relativa, contenido in archivos.items():
        ruta = base / relativa
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(textwrap.dedent(contenido), encoding="utf-8")


CrearRepo = Callable[..., Path]


@pytest.fixture(autouse=True)
def _git_aislado(monkeypatch: pytest.MonkeyPatch) -> None:
    for clave, valor in ENTORNO_GIT.items():
        monkeypatch.setenv(clave, valor)
    monkeypatch.delenv("PY_BACKSTAGE_CATALOGO", raising=False)


@pytest.fixture
def clones(tmp_path: Path) -> Path:
    ruta = tmp_path / "clones"
    ruta.mkdir()
    return ruta


@pytest.fixture
def crear_repo(clones: Path) -> CrearRepo:
    """Repo con un commit y ``origin/<rama>`` apuntando a él (como tras un fetch)."""

    def _crear(nombre: str, archivos: dict[str, str], rama: str = "main") -> Path:
        ruta = clones / nombre
        ruta.mkdir(parents=True)
        git(ruta, "init", "-q", "-b", rama)
        escribir(ruta, archivos)
        git(ruta, "add", "-A")
        git(ruta, "commit", "-q", "--no-gpg-sign", "-m", "inicial")
        git(ruta, "update-ref", f"refs/remotes/origin/{rama}", "HEAD")
        git(ruta, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{rama}")
        return ruta

    return _crear


CrearCatalogo = Callable[..., Path]


@pytest.fixture
def crear_catalogo(tmp_path: Path, clones: Path) -> CrearCatalogo:
    def _crear(config: str, entidades: dict[str, str], nombre: str = "catalogo") -> Path:
        base = tmp_path / nombre
        base.mkdir()
        escribir(base, {"py-backstage.yaml": f"raiz_clones: {clones}\n" + textwrap.dedent(config)})
        escribir(base, entidades)
        return base

    return _crear


BASE = """\
apiVersion: backstage.io/v1alpha1
kind: Group
metadata:
  name: plataforma
  description: Equipo
spec:
  type: team
  children: []
---
apiVersion: backstage.io/v1alpha1
kind: System
metadata:
  name: tienda
  description: Tienda
spec:
  owner: plataforma
---
apiVersion: backstage.io/v1alpha1
kind: API
metadata:
  name: pedidos-sql
  description: Tablas de pedidos
  annotations:
    py-backstage/huellas-sql: pedidos_
spec:
  type: sql
  lifecycle: production
  owner: plataforma
  system: tienda
  definition: pedidos_pedido
---
apiVersion: backstage.io/v1alpha1
kind: API
metadata:
  name: facturas-http
  description: API de facturas
  annotations:
    py-backstage/huellas: \\bFACTURADOR_URL\\b
    py-backstage/variables: |
      FACTURADOR_URL=http://facturador.interno:8000
spec:
  type: openapi
  lifecycle: production
  owner: plataforma
  system: tienda
  definition: "openapi: 3.1.0"
---
apiVersion: backstage.io/v1alpha1
kind: Resource
metadata:
  name: sin-huellas
  description: Recurso sin huellas declaradas
spec:
  type: queue
  owner: plataforma
  system: tienda
---
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: tienda-web
  description: Tienda
  annotations:
    github.com/project-slug: ejemplo/tienda
spec:
  type: service
  lifecycle: production
  owner: plataforma
  system: tienda
  providesApis: [pedidos-sql]
"""


def componente(nombre: str, repo: str, **spec: object) -> str:
    """Un Component en YAML (``ruta`` va como anotación py-backstage/ruta)."""
    ruta = spec.pop("ruta", None)
    lineas = [
        "---",
        "apiVersion: backstage.io/v1alpha1",
        "kind: Component",
        "metadata:",
        f"  name: {nombre}",
        f"  description: Componente {nombre}",
        "  annotations:",
        f"    github.com/project-slug: {repo}",
    ]
    if ruta:
        lineas.append(f"    py-backstage/ruta: {ruta}")
    lineas += [
        "spec:",
        "  type: service",
        "  lifecycle: production",
        "  owner: plataforma",
        "  system: tienda",
    ]
    for clave, valor in spec.items():
        lineas.append(f"  {clave}: {valor}")
    return "\n".join(lineas) + "\n"
