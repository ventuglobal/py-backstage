"""``nuevo-servicio``: crea un repo desde la plantilla Copier ya acoplado al catálogo.

Lo que hace "automático" el acople de un proyecto nuevo:

1. valida contra el catálogo el sistema, el dueño y cada dependencia pedida;
2. escribe el ``catalog-info.yaml`` del servicio con esas referencias;
3. trae de cada dependencia sus variables (anotación ``py-backstage/variables``,
   p. ej. ``FACTURADOR_URL=http://facturador.interno:8000``)
   al ``.env.example`` y al README;
4. deja ``.copier-answers.yml`` para que ``copier update`` traiga las mejoras
   de la plantilla.

Se genera en una carpeta temporal junto al destino y se mueve sólo si el
``catalog-info.yaml`` resultante valida: un error no deja el destino a medias.
"""

from __future__ import annotations

import keyword
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .catalogo import ORG, Catalogo, parsear_entidades
from .modelo import ANOT_VARIABLES, Ref, parsear_ref
from .repos import ErrorGit, Repo

PLANTILLA_PUBLICA = "https://github.com/ventuglobal/py-backstage.git"
_NOMBRE = re.compile(r"[a-z][a-z0-9-]{1,40}[a-z0-9]")
_VARIABLE = re.compile(r"[A-Z][A-Z0-9_]*=[^\x00-\x1f\x7f]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Lo que instala la plantilla. Con la instalación editable, un paquete instalado
# (o de la biblioteca estándar) con el mismo nombre gana al del servicio y
# `uv run <servicio>` falla con ModuleNotFoundError.
DEPENDENCIAS_PLANTILLA = frozenset(
    {
        "annotated_types", "anyio", "click", "dotenv", "fastapi", "h11", "hatchling",
        "httpcore", "httpx", "idna", "iniconfig", "mypy", "mypy_extensions", "packaging",
        "pathspec", "pluggy", "psycopg", "psycopg_binary", "psycopg_pool", "pydantic",
        "pydantic_core", "pydantic_settings", "pytest", "ruff", "sniffio", "starlette",
        "typing_extensions", "typing_inspection", "uvicorn",
    }
)  # fmt: skip
# Variables que la plantilla ya escribe en .env.example (y lee config.py).
VARIABLES_PLANTILLA = ("LOG_LEVEL", "HOST", "PORT")


class ErrorNuevo(RuntimeError):
    pass


@dataclass
class Pedido:
    nombre: str
    descripcion: str
    sistema: str
    dueno: str
    org: str
    dependencias: list[str]
    usa_postgres: bool = False


def plantilla_local() -> Path | None:
    """La raíz del checkout de py-backstage si se corre desde él."""
    for carpeta in Path(__file__).resolve().parents:
        if (carpeta / "copier.yml").exists() and (carpeta / "plantillas").is_dir():
            return carpeta
    return None


def paquete_reservado(paquete: str) -> bool:
    """Si el paquete Python del servicio chocaría con Python o con sus dependencias."""
    return (
        keyword.iskeyword(paquete)
        or paquete in sys.stdlib_module_names
        or paquete in DEPENDENCIAS_PLANTILLA
    )


def _sin_tipo(ref: Ref) -> str:
    """``name`` o ``namespace/name``: el tipo lo da el campo (``spec.system``)."""
    return ref.name if ref.namespace == "default" else f"{ref.namespace}/{ref.name}"


def preparar(catalogo: Catalogo | None, pedido: Pedido) -> dict[str, Any]:
    """Respuestas para Copier, validadas contra el catálogo (si se entrega)."""
    if not _NOMBRE.fullmatch(pedido.nombre):
        raise ErrorNuevo("el nombre va en minúsculas con guiones (p. ej. 'cotizador-web')")
    paquete = pedido.nombre.replace("-", "_")
    if paquete_reservado(paquete):
        raise ErrorNuevo(
            f"el paquete {paquete!r} chocaría con un módulo de Python o de las dependencias "
            "de la plantilla: elegir otro nombre (p. ej. 'servicio-" + pedido.nombre + "')"
        )
    descripcion = pedido.descripcion.strip()
    if not descripcion or _CONTROL.search(descripcion):
        raise ErrorNuevo("la descripción es obligatoria, en una línea y sin caracteres de control")
    if not ORG.fullmatch(pedido.org):
        raise ErrorNuevo(f"organización de GitHub inválida {pedido.org!r}")
    if catalogo is not None and parsear_ref(pedido.nombre, "component") in catalogo.entidades:
        raise ErrorNuevo(f"component:{pedido.nombre} ya existe en el catálogo")
    try:
        sistema = parsear_ref(pedido.sistema, "system")
        dueno = parsear_ref(pedido.dueno, "group")
    except ValueError as exc:
        raise ErrorNuevo(str(exc)) from exc
    dependencias = []
    variables: dict[str, dict[str, str]] = {}
    errores = []
    if sistema.kind != "system":
        errores.append(f"el sistema debe ser un system, no {sistema.corta}")
    if dueno.kind not in {"group", "user"}:
        errores.append(f"el dueño debe ser un group o user, no {dueno.corta}")
    for texto in pedido.dependencias:
        try:
            ref = parsear_ref(texto)
        except ValueError as exc:
            errores.append(str(exc))
            continue
        if ref.kind not in {"api", "resource", "component"}:
            errores.append(f"{ref.corta}: sólo se depende de APIs, recursos o componentes")
            continue
        dependencias.append(ref)
    propias = set(VARIABLES_PLANTILLA) | ({"DATABASE_URL"} if pedido.usa_postgres else set())
    if catalogo is not None:
        faltan = False
        for ref, que in ((sistema, "sistema"), (dueno, "dueño")):
            if ref not in catalogo.entidades:
                errores.append(f"el {que} {ref.corta} no existe en el catálogo")
                faltan = True
        for ref in dependencias:
            entidad = catalogo.obtener(ref)
            if entidad is None:
                errores.append(f"la dependencia {ref.corta} no existe en el catálogo")
                faltan = True
                continue
            for linea in entidad.lista_anotacion(ANOT_VARIABLES, separador="\n"):
                if not _VARIABLE.fullmatch(linea):
                    errores.append(f"{ref.corta}: variable mal escrita {linea!r} (NOMBRE=valor)")
                    continue
                nombre, _, valor = linea.partition("=")
                variable = {"linea": linea, "fuente": ref.corta}
                # Comentadas: no deben pisar la de la plantilla ni llegar a un .env
                # local como referencia de la plataforma (python-dotenv la rompe).
                if nombre in propias:
                    variable["nota"] = "choca con una variable de este servicio"
                elif "${" in valor:
                    variable["nota"] = "referencia de la plataforma: en local, poner el valor"
                variables.setdefault(nombre, variable)
        if faltan and not catalogo.descriptores_leidos:
            errores.append("el catálogo se cargó sin leer los catalog-info.yaml de los repos")
        elif faltan and catalogo.no_leidos:
            errores.append(
                "no se leyeron los descriptores de " + ", ".join(sorted(catalogo.no_leidos))
            )
    if errores:
        raise ErrorNuevo("; ".join(errores))
    return {
        "nombre": pedido.nombre,
        "descripcion": descripcion,
        "sistema": _sin_tipo(sistema),
        "dueno": dueno.corta,
        "org": pedido.org,
        "depende_de": [r.corta for r in dependencias if r.kind != "api"],
        "consume_apis": [r.corta for r in dependencias if r.kind == "api"],
        "variables": list(variables.values()),
        "usa_postgres": pedido.usa_postgres,
    }


def _carpeta(fuente: str) -> Path | None:
    """La fuente como carpeta absoluta, o ``None`` si es una URL git."""
    try:
        ruta = Path(fuente).expanduser()
        return ruta.resolve() if ruta.is_dir() else None
    except OSError:
        return None


def resolver_fuente(plantilla: str | None, ref: str | None) -> tuple[str, str | None]:
    """Fuente y versión que usará Copier.

    Una carpeta queda como ruta absoluta: relativa, ``copier update`` la
    buscaría desde el proyecto generado.
    """
    fuente = plantilla or PLANTILLA_PUBLICA
    carpeta = _carpeta(fuente)
    if carpeta is not None:
        fuente = str(carpeta)
    if ref is None:
        local = plantilla_local()
        if local is not None and carpeta == local:
            ref = "HEAD"
        elif fuente == PLANTILLA_PUBLICA:
            ref = f"v{__version__}"
    return fuente, ref


def aviso_plantilla_sucia(plantilla: str | None, ref: str | None) -> str | None:
    """Aviso si se usará una carpeta git con cambios sin commitear.

    Copier los toma en un commit temporal: ``_commit`` en
    ``.copier-answers.yml`` queda con un SHA que no existe y ``copier update``
    falla hasta recrear el proyecto.
    """
    fuente, ref = resolver_fuente(plantilla, ref)
    if ref != "HEAD" or _carpeta(fuente) is None:
        return None
    try:
        estado = Repo("plantilla", Path(fuente))._git(
            "status", "--porcelain", "--", "copier.yml", "plantillas"
        )
    except ErrorGit:
        return None
    if not estado.strip():
        return None
    return (
        f"aviso: la plantilla {fuente} tiene cambios sin commitear; el proyecto los "
        "incluye, pero copier update no funcionará en él (commitearlos y recrearlo)"
    )


def crear(
    destino: Path,
    datos: dict[str, Any],
    plantilla: str | None = None,
    ref: str | None = None,
) -> str:
    """Corre Copier y devuelve la fuente usada (queda en ``.copier-answers.yml``).

    Sin ``ref``, la plantilla pública se toma en el tag de esta versión
    (``v{__version__}``), así la CLI y la plantilla siempre calzan; el checkout
    local de py-backstage, en ``HEAD`` con los cambios sin commitear; otra
    fuente git, en su último tag.
    """
    try:
        from copier import run_copy
    except ImportError as exc:  # pragma: no cover - depende de la instalación
        raise ErrorNuevo("falta Copier: instalar py-backstage[plantillas]") from exc
    destino = destino.expanduser()
    if destino.exists() and not destino.is_dir():
        raise ErrorNuevo(f"{destino} no es una carpeta")
    if destino.exists() and any(destino.iterdir()):
        raise ErrorNuevo(f"{destino} no está vacía")
    fuente, ref = resolver_fuente(plantilla, ref)
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = Path(tempfile.mkdtemp(prefix=f".{destino.name}-", dir=destino.parent))
    try:
        run_copy(
            fuente,
            temporal,
            data=datos,
            vcs_ref=ref,
            defaults=True,
            unsafe=False,
            quiet=True,
        )
        descriptor = temporal / "catalog-info.yaml"
        if not descriptor.is_file():
            raise ErrorNuevo("la plantilla no generó catalog-info.yaml")
        entidades, problemas = parsear_entidades(
            descriptor.read_text(encoding="utf-8"), "catalog-info.yaml"
        )
        if problemas or len(entidades) != 1:
            detalle = "; ".join(p.mensaje for p in problemas) or f"{len(entidades)} entidades"
            raise ErrorNuevo(f"la plantilla generó un catalog-info.yaml inválido: {detalle}")
        if destino.exists():
            destino.rmdir()
        temporal.rename(destino)
    finally:
        shutil.rmtree(temporal, ignore_errors=True)
    return fuente
