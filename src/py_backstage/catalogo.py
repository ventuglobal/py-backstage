"""Carga y validación del catálogo.

Un catálogo es una carpeta (o una carpeta dentro de un repo git) con:

- ``py-backstage.yaml``: la configuración (organización y repos a auditar);
- cualquier otro ``*.yaml`` / ``*.yml``: entidades en formato Backstage (uno o
  varios documentos por archivo).

Además se lee el ``catalog-info.yaml`` de la raíz de cada repo configurado, en
``origin/<rama>``, si existe: el descriptor puede vivir en el catálogo central o
en el propio repo (lo habitual en Backstage).
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .modelo import (
    ANOT_GITHUB,
    ANOT_PROVISTO_POR,
    ESPECS,
    Entidad,
    Ref,
    describir_error,
    parsear_ref,
    sin_control,
)
from .repos import DESCRIPTOR, ErrorGit, Repo

CONFIG = "py-backstage.yaml"
VARIABLE_CATALOGO = "PY_BACKSTAGE_CATALOGO"

# Organización de GitHub: letras, números y guiones; máximo 39.
ORG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
# Lo que git rechaza en un nombre de rama (git check-ref-format), más un '-'
# inicial, que se leería como opción.
_RAMA_PROHIBIDA = re.compile(r"\.\.|@\{|//|[\x00-\x20\x7f~^:?*\[\\]|\.lock(?:/|$)|/\.")


class ErrorCatalogo(RuntimeError):
    pass


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    ruta: str | None = None  # relativa a raiz_clones; por defecto, el nombre del repo
    rama: str | None = None
    excluir: list[str] = Field(default_factory=list)
    # Repos que existen pero no se auditan (archivados, forks, demos).
    ignorar: bool = False
    motivo: str | None = None

    @field_validator("slug")
    @classmethod
    def _slug(cls, valor: str) -> str:
        # '.' o '..' como nombre apuntarían fuera de raiz_clones.
        if any(parte in {".", ".."} for parte in valor.split("/")):
            raise ValueError(f"slug inválido {valor!r}")
        return valor

    @field_validator("rama")
    @classmethod
    def _rama(cls, valor: str | None) -> str | None:
        if valor is None:
            return None
        if (
            not valor
            or valor.startswith(("-", "/", "."))
            or valor.endswith(("/", "."))
            or _RAMA_PROHIBIDA.search(valor)
        ):
            raise ValueError(f"rama inválida {valor!r}")
        return valor

    @field_validator("ruta")
    @classmethod
    def _ruta(cls, valor: str | None) -> str | None:
        if valor is None:
            return None
        partes = valor.split("/")
        if not valor or valor.startswith(("/", "~")) or ".." in partes or "\\" in valor:
            raise ValueError(f"ruta inválida {valor!r}: va relativa a raiz_clones, sin '..' ni '~'")
        return valor


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org: str | None = None
    raiz_clones: str = "~/code"
    repos: list[RepoConfig] = Field(default_factory=list)

    @field_validator("org")
    @classmethod
    def _org(cls, valor: str | None) -> str | None:
        if valor is not None and not ORG.fullmatch(valor):
            raise ValueError(f"org inválida {valor!r} (letras, números y '-'; máximo 39)")
        return valor

    @model_validator(mode="after")
    def _sin_repetidos(self) -> Config:
        # GitHub no distingue mayúsculas: dos entradas serían el mismo repo
        # auditado dos veces.
        vistos: set[str] = set()
        for repo in self.repos:
            clave = repo.slug.lower()
            if clave in vistos:
                raise ValueError(f"el repo {repo.slug!r} aparece más de una vez en repos")
            vistos.add(clave)
        return self

    def ruta_de(self, repo: RepoConfig, base: Path) -> Path:
        nombre = repo.ruta or repo.slug.split("/", 1)[1]
        raiz = Path(os.path.expanduser(self.raiz_clones))
        if not raiz.is_absolute():
            raiz = base / raiz
        # Sin seguir enlaces: un clon puede ser un enlace simbólico a otra carpeta.
        ruta = Path(os.path.normpath(raiz / nombre))
        if not ruta.is_relative_to(os.path.normpath(raiz)):
            raise ErrorCatalogo(f"{repo.slug}: la ruta {nombre!r} sale de raiz_clones")
        return ruta.resolve()


# Campos con referencias y el kind que se asume si no se escribe.
CAMPOS_REF: dict[str, list[tuple[str, str | None, bool]]] = {
    # campo, kind por defecto, ¿lista?
    "Component": [
        ("owner", "group", False),
        ("system", "system", False),
        ("subcomponentOf", "component", False),
        ("providesApis", "api", True),
        ("consumesApis", "api", True),
        ("dependsOn", None, True),
        ("dependencyOf", None, True),
    ],
    "API": [("owner", "group", False), ("system", "system", False)],
    "Resource": [
        ("owner", "group", False),
        ("system", "system", False),
        ("dependsOn", None, True),
        ("dependencyOf", None, True),
    ],
    "System": [("owner", "group", False), ("domain", "domain", False)],
    "Domain": [("owner", "group", False), ("subdomainOf", "domain", False)],
    "Group": [("parent", "group", False), ("children", "group", True), ("members", "user", True)],
    "User": [("memberOf", "group", True)],
    "Location": [],
}


@dataclass
class Problema:
    """Error de formato o de referencias (lo que haría fallar la ingesta)."""

    origen: str
    mensaje: str
    entidad: str | None = None

    def __str__(self) -> str:
        quien = f" [{self.entidad}]" if self.entidad else ""
        return sin_control(f"{self.origen}{quien}: {self.mensaje}")


@dataclass
class Catalogo:
    base: Path
    config: Config
    entidades: dict[Ref, Entidad] = field(default_factory=dict)
    problemas: list[Problema] = field(default_factory=list)
    # repo slug -> (ref git, sha, fecha) de donde se leyó su descriptor
    leidos: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    no_leidos: dict[str, str] = field(default_factory=dict)
    # False si se cargó sin leer los catalog-info.yaml de los repos.
    descriptores_leidos: bool = True

    # ── consultas ──

    def de_tipo(self, kind: str) -> list[Entidad]:
        kind = kind.lower()
        return sorted((e for r, e in self.entidades.items() if r.kind == kind), key=lambda e: e.ref)

    def obtener(self, ref: Ref) -> Entidad | None:
        return self.entidades.get(ref)

    def refs(self, entidad: Entidad, campo: str) -> list[Ref]:
        """Referencias de ``spec.<campo>`` normalizadas (las inválidas se omiten).

        Sin namespace explícito, una referencia apunta al namespace de la
        entidad que la escribe.
        """
        for nombre, por_defecto, es_lista in CAMPOS_REF.get(entidad.kind, []):
            if nombre != campo:
                continue
            valor = entidad.spec.get(campo)
            valores = valor if es_lista and isinstance(valor, list) else [valor]
            salida = []
            for texto in valores:
                if not isinstance(texto, str):
                    continue
                try:
                    salida.append(parsear_ref(texto, por_defecto, entidad.metadata.namespace))
                except ValueError:
                    continue
            return salida
        return []

    def dependencias(self, entidad: Entidad) -> list[Ref]:
        """Todo lo que la entidad declara usar.

        Sus ``dependsOn`` y ``consumesApis``, más las entidades que la nombran en
        su ``dependencyOf`` (la misma relación escrita desde el otro lado).
        """
        salida = set(self.refs(entidad, "dependsOn") + self.refs(entidad, "consumesApis"))
        for otra in self.entidades.values():
            if otra.kind in {"Component", "Resource"} and entidad.ref in self.refs(
                otra, "dependencyOf"
            ):
                salida.add(otra.ref)
        return sorted(salida)

    def proveedores(self, objetivo: Entidad) -> set[Ref]:
        """Componentes que proveen una API o un recurso (exentos de declarar su uso).

        Los que lo declaran en ``providesApis``, los que el objetivo nombra como
        dependientes de él (su ``dependsOn`` apunta al componente, o el
        ``dependencyOf`` del componente apunta al objetivo) y los de la anotación
        ``py-backstage/provisto-por``.
        """
        salida: set[Ref] = set()
        for componente in self.de_tipo("component"):
            if objetivo.ref in self.refs(componente, "providesApis"):
                salida.add(componente.ref)
            if objetivo.ref in self.refs(componente, "dependencyOf"):
                salida.add(componente.ref)
        for ref in self.refs(objetivo, "dependsOn"):
            if ref.kind == "component":
                salida.add(ref)
        for texto in objetivo.lista_anotacion(ANOT_PROVISTO_POR):
            try:
                salida.add(parsear_ref(texto, "component", objetivo.metadata.namespace))
            except ValueError:
                continue
        return salida

    def componentes_de_repo(self, slug: str) -> list[Entidad]:
        slug = slug.lower()
        return [
            c for c in self.de_tipo("component") if (c.anotacion(ANOT_GITHUB) or "").lower() == slug
        ]

    def repo(self, slug: str) -> Repo | None:
        for repo in self.config.repos:
            if repo.slug.lower() == slug.lower():
                return Repo(repo.slug, self.config.ruta_de(repo, self.base), repo.rama)
        return None


# ── lectura ─────────────────────────────────────────────────────────────────


def _documentos(texto: str, origen: str, problemas: list[Problema]) -> list[dict[str, Any]]:
    try:
        documentos = [d for d in yaml.safe_load_all(texto) if d is not None]
    except yaml.YAMLError as exc:
        problemas.append(Problema(origen, f"YAML inválido: {exc}"))
        return []
    salida = []
    for i, documento in enumerate(documentos):
        if not isinstance(documento, dict):
            problemas.append(Problema(origen, f"documento {i + 1}: se esperaba un mapa"))
            continue
        salida.append(documento)
    return salida


def parsear_entidades(texto: str, origen: str) -> tuple[list[Entidad], list[Problema]]:
    """Entidades de un YAML (uno o varios documentos) y sus problemas de formato."""
    problemas: list[Problema] = []
    entidades = []
    for documento in _documentos(texto, origen, problemas):
        nombre = (
            (documento.get("metadata") or {}).get("name")
            if isinstance(documento.get("metadata"), dict)
            else None
        )
        quien = f"{documento.get('kind', '?')}:{nombre or '?'}"
        try:
            entidad = Entidad.model_validate(documento)
        except ValidationError as exc:
            for mensaje in describir_error(exc):
                problemas.append(Problema(origen, mensaje, quien))
            continue
        try:
            ESPECS[entidad.kind].model_validate(entidad.spec)
        except ValidationError as exc:
            for mensaje in describir_error(exc):
                problemas.append(Problema(origen, f"spec.{mensaje}", quien))
            continue
        entidad.origen = origen
        entidades.append(entidad)
    return entidades, problemas


def _es_yaml_del_catalogo(relativa: str) -> bool:
    """``*.yaml``/``*.yml`` fuera de carpetas o archivos ocultos (``.github/``...)."""
    partes = relativa.split("/")
    return relativa.endswith((".yaml", ".yml")) and not any(p.startswith(".") for p in partes)


def _decodificar(datos: bytes, origen: str, problemas: list[Problema]) -> str | None:
    try:
        return datos.decode("utf-8")
    except UnicodeDecodeError:
        problemas.append(Problema(origen, "no es UTF-8"))
        return None


def _leer_carpeta(base: Path) -> tuple[bytes | None, list[tuple[str, bytes]]]:
    if not base.is_dir():
        raise ErrorCatalogo(f"no existe la carpeta del catálogo: {base}")
    config = base / CONFIG
    archivos = sorted(
        p
        for p in base.rglob("*")
        if p.is_file() and _es_yaml_del_catalogo(p.relative_to(base).as_posix())
    )
    textos = [(p.relative_to(base).as_posix(), p.read_bytes()) for p in archivos if p != config]
    return (config.read_bytes() if config.exists() else None), textos


def _leer_git(espec: str) -> tuple[Path, bytes | None, list[tuple[str, bytes]]]:
    """``git:<repo>@<ref>:<carpeta>`` → lee el catálogo desde los objetos de git."""
    cuerpo = espec.removeprefix("git:")
    repo_y_ref, sep, carpeta = cuerpo.rpartition(":")
    # La ruta y el ref pueden llevar '@' (``HEAD@{1}``): se corta en el primer
    # '@' que deja a la izquierda un repo git y, si no hay, en el último.
    arrobas = [i for i, c in enumerate(repo_y_ref) if c == "@"]
    corte = next(
        (i for i in arrobas if (Path(os.path.expanduser(repo_y_ref[:i])) / ".git").exists()),
        arrobas[-1] if arrobas else -1,
    )
    ruta_repo, referencia = repo_y_ref[: max(corte, 0)], repo_y_ref[corte + 1 :]
    if not sep or corte < 0 or not ruta_repo or not referencia:
        raise ErrorCatalogo("formato esperado: git:<ruta del repo>@<ref>:<carpeta>")
    repo = Repo("catalogo", Path(os.path.expanduser(ruta_repo)).resolve())
    carpeta = carpeta.strip("/")
    try:
        archivos = repo.archivos(referencia)
    except ErrorGit as exc:
        raise ErrorCatalogo(str(exc)) from exc
    prefijo = f"{carpeta}/" if carpeta else ""
    seleccion = [
        a
        for a in archivos
        if a.ruta.startswith(prefijo) and _es_yaml_del_catalogo(a.ruta[len(prefijo) :])
    ]
    if not seleccion:
        raise ErrorCatalogo(f"no hay archivos YAML en {carpeta or '.'} de {referencia}")
    config = None
    textos = []
    for archivo, contenido in repo.leer_bytes(seleccion):
        relativa = archivo.ruta[len(prefijo) :]
        if relativa == CONFIG:
            config = contenido
        else:
            textos.append((relativa, contenido))
    return repo.ruta, config, sorted(textos)


def cargar(fuente: str | Path | None = None, *, leer_repos: bool = True) -> Catalogo:
    """Carga el catálogo de una carpeta o de ``git:<repo>@<ref>:<carpeta>``.

    Con ``leer_repos`` también lee el ``catalog-info.yaml`` de cada repo.
    """
    fuente = fuente or os.environ.get(VARIABLE_CATALOGO)
    if not fuente:
        raise ErrorCatalogo(f"falta --catalogo (o la variable {VARIABLE_CATALOGO})")
    fuente = str(fuente)
    if fuente.startswith("git:"):
        base, bytes_config, archivos = _leer_git(fuente)
    else:
        base = Path(os.path.expanduser(fuente)).resolve()
        bytes_config, archivos = _leer_carpeta(base)

    problemas: list[Problema] = []
    texto_config = None
    if bytes_config is not None:
        try:
            texto_config = bytes_config.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ErrorCatalogo(f"{CONFIG}: no es UTF-8") from exc
    textos = []
    for origen, datos in archivos:
        texto = _decodificar(datos, origen, problemas)
        if texto is not None:
            textos.append((origen, texto))
    datos_config: Any = {}
    if texto_config is not None:
        try:
            datos_config = yaml.safe_load(texto_config) or {}
        except yaml.YAMLError as exc:
            raise ErrorCatalogo(f"{CONFIG}: YAML inválido: {exc}") from exc
    try:
        config = Config.model_validate(datos_config)
    except ValidationError as exc:
        raise ErrorCatalogo(f"{CONFIG}: " + "; ".join(describir_error(exc))) from exc

    catalogo = Catalogo(base=base, config=config, descriptores_leidos=leer_repos)
    todas: list[Entidad] = []
    for origen, texto in textos:
        entidades, errores = parsear_entidades(texto, origen)
        todas += entidades
        problemas += errores

    if leer_repos:
        for repo_config in config.repos:
            if repo_config.ignorar:
                continue
            repo = Repo(repo_config.slug, config.ruta_de(repo_config, base), repo_config.rama)
            if not repo.disponible:
                catalogo.no_leidos[repo.slug] = f"sin clon local en {repo.ruta}"
                continue
            try:
                referencia = repo.referencia()
                sha, fecha = repo.commit(referencia)
            except ErrorGit as exc:
                catalogo.no_leidos[repo.slug] = str(exc)
                continue
            catalogo.leidos[repo.slug] = (referencia, sha, fecha)
            origen = f"{repo.slug}:{DESCRIPTOR}"
            crudo = repo.leer_ruta_bytes(referencia, DESCRIPTOR)
            if crudo is None:
                continue
            descriptor = _decodificar(crudo, origen, problemas)
            if descriptor is None:
                continue
            entidades, errores = parsear_entidades(descriptor, origen)
            for entidad in entidades:
                if entidad.kind == "Component" and not entidad.anotacion(ANOT_GITHUB):
                    entidad.metadata.annotations[ANOT_GITHUB] = repo.slug
            todas += entidades
            problemas += errores

    vistos: dict[Ref, Entidad] = {}
    for entidad in todas:
        previa = vistos.get(entidad.ref)
        if previa is not None:
            problemas.append(
                Problema(
                    entidad.origen,
                    f"duplicada: ya definida en {previa.origen}",
                    entidad.ref.corta,
                )
            )
            continue
        vistos[entidad.ref] = entidad
    catalogo.entidades = vistos
    catalogo.problemas = problemas + _validar_referencias(catalogo)
    return catalogo


def _validar_referencias(catalogo: Catalogo) -> list[Problema]:
    problemas = []
    sin_leer = _descriptores_sin_leer(catalogo)
    nota = (
        f" (no se leyeron los descriptores de {sin_leer} repos: puede estar en alguno)"
        if sin_leer
        else ""
    )
    for entidad in catalogo.entidades.values():
        for campo, por_defecto, es_lista in CAMPOS_REF.get(entidad.kind, []):
            valor = entidad.spec.get(campo)
            if valor is None:
                continue
            valores = valor if es_lista else [valor]
            if not isinstance(valores, list):
                continue
            for texto in valores:
                try:
                    ref = parsear_ref(str(texto), por_defecto, entidad.metadata.namespace)
                except ValueError as exc:
                    problemas.append(
                        Problema(entidad.origen, f"spec.{campo}: {exc}", entidad.ref.corta)
                    )
                    continue
                if ref not in catalogo.entidades:
                    problemas.append(
                        Problema(
                            entidad.origen,
                            f"spec.{campo}: {ref.corta} no existe en el catálogo{nota}",
                            entidad.ref.corta,
                        )
                    )
    return problemas


def _descriptores_sin_leer(catalogo: Catalogo) -> int:
    """Repos configurados (no ignorados) cuyo ``catalog-info.yaml`` no se leyó."""
    if not catalogo.descriptores_leidos:
        return sum(1 for r in catalogo.config.repos if not r.ignorar)
    return len(catalogo.no_leidos)


def descriptor_sin_leer(catalogo: Catalogo, slug: str) -> bool:
    """Si el ``catalog-info.yaml`` de ``slug`` pudo no haberse leído."""
    if not catalogo.descriptores_leidos:
        return True
    return any(s.lower() == slug.lower() for s in catalogo.no_leidos)


def relaciones(catalogo: Catalogo) -> dict[Ref, set[tuple[str, Ref]]]:
    """Aristas salientes por entidad: (tipo de relación, destino)."""
    aristas: dict[Ref, set[tuple[str, Ref]]] = defaultdict(set)
    for entidad in catalogo.entidades.values():
        for campo in ("dependsOn", "consumesApis", "providesApis", "subcomponentOf"):
            for destino in catalogo.refs(entidad, campo):
                if destino in catalogo.entidades:
                    aristas[entidad.ref].add((campo, destino))
        for destino in catalogo.refs(entidad, "dependencyOf"):
            if destino in catalogo.entidades:
                aristas[destino].add(("dependsOn", entidad.ref))
    return aristas
