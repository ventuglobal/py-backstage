"""Reglas de gobierno del catálogo.

V15  Acople no declarado: el código de un repo usa una API o un recurso del
     catálogo (según sus huellas) y ningún componente de ese repo lo declara
     en ``dependsOn`` / ``consumesApis`` (ni lo provee).
V16  Sin descriptor o con metadatos incompletos: repo configurado sin
     componente, entidad sin descripción / sistema / repo, YAML, referencias o
     huellas inválidas, ``py-backstage/ruta`` sin archivos.
V17  Dependencia declarada sin evidencia: un componente declara usar algo con
     huellas y su código no muestra ninguna (las plantillas ``.env.example``
     no cuentan: declarar una variable no es usarla).

Cada hallazgo trae la evidencia ``repo:archivo:línea`` leída en
``origin/<rama>``. Los repos sin clon local se informan como «no auditados»:
nunca se omiten en silencio.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from .catalogo import Catalogo, descriptor_sin_leer
from .huellas import Coincidencia, Huellas, buscar, compilar
from .modelo import (
    ANOT_EXCLUIR,
    ANOT_GITHUB,
    ANOT_RUTA,
    ESQUEMAS_ENLACE,
    Entidad,
    Ref,
    enlace_seguro,
)
from .repos import ErrorGit, Repo, candidatos_evidencia, es_plantilla_env

REGLAS = {
    "V15": ("S2", "Acople no declarado"),
    "V16": ("S3", "Sin descriptor o metadatos incompletos"),
    "V17": ("S3", "Dependencia declarada sin evidencia"),
}
MAX_EVIDENCIAS = 5


@dataclass
class Evidencia:
    repo: str
    archivo: str
    linea: int
    extracto: str

    def __str__(self) -> str:
        return f"{self.repo}:{self.archivo}:{self.linea}  {self.extracto}"


@dataclass
class Hallazgo:
    regla: str
    mensaje: str
    entidad: str | None = None
    objetivo: str | None = None
    repo: str | None = None
    origen: str | None = None
    evidencias: list[Evidencia] = field(default_factory=list)
    total_evidencias: int = 0

    @property
    def severidad(self) -> str:
        return REGLAS[self.regla][0]

    @property
    def clave(self) -> str:
        """Identidad estable (para comparar barridos)."""
        return ":".join(p or "-" for p in (self.regla, self.repo, self.entidad, self.objetivo))

    def como_dict(self) -> dict[str, Any]:
        datos = asdict(self)
        datos["severidad"] = self.severidad
        datos["titulo"] = REGLAS[self.regla][1]
        datos["clave"] = self.clave
        return datos


@dataclass
class RepoAuditado:
    slug: str
    referencia: str
    commit: str
    fecha: str
    archivos: int


@dataclass
class Resultado:
    hallazgos: list[Hallazgo] = field(default_factory=list)
    auditados: list[RepoAuditado] = field(default_factory=list)
    no_auditados: dict[str, str] = field(default_factory=dict)
    # Dependencias declaradas hacia entidades sin huellas: no verificables.
    sin_huellas: list[tuple[str, str]] = field(default_factory=list)

    def como_dict(self) -> dict[str, Any]:
        return {
            "hallazgos": [h.como_dict() for h in self.hallazgos],
            "auditados": [asdict(r) for r in self.auditados],
            "no_auditados": self.no_auditados,
            "sin_huellas": [{"entidad": e, "objetivo": o} for e, o in self.sin_huellas],
        }


# ── V16 ─────────────────────────────────────────────────────────────────────


def _objetivos(catalogo: Catalogo) -> list[Entidad]:
    """Entidades que pueden declarar huellas."""
    return [e for e in catalogo.entidades.values() if e.kind in {"API", "Resource", "Component"}]


def revisar_metadatos(catalogo: Catalogo) -> list[Hallazgo]:
    hallazgos = [
        Hallazgo("V16", p.mensaje, entidad=p.entidad, origen=p.origen) for p in catalogo.problemas
    ]
    # Una huella inválida es un error del descriptor: `validar` también la informa.
    for ref, mensaje in compilar(_objetivos(catalogo)).errores:
        entidad = catalogo.obtener(ref)
        hallazgos.append(
            Hallazgo("V16", mensaje, entidad=ref.corta, origen=entidad.origen if entidad else None)
        )

    def falta(entidad: Entidad, que: str) -> None:
        hallazgos.append(
            Hallazgo("V16", f"falta {que}", entidad=entidad.ref.corta, origen=entidad.origen)
        )

    for entidad in sorted(catalogo.entidades.values(), key=lambda e: e.ref):
        if entidad.kind in {"Location", "User"}:
            continue
        if not (entidad.metadata.description or "").strip():
            falta(entidad, "metadata.description")
        if entidad.kind in {"Component", "API", "Resource"} and not entidad.spec.get("system"):
            falta(entidad, "spec.system")
        if entidad.kind == "Component" and not entidad.anotacion(ANOT_GITHUB):
            falta(entidad, f"la anotación {ANOT_GITHUB} (repo del componente)")
        for enlace in entidad.metadata.links:
            if enlace_seguro(enlace.url) is None:
                hallazgos.append(
                    Hallazgo(
                        "V16",
                        f"metadata.links: {enlace.url[:80]!r} no es un enlace "
                        f"{', '.join(ESQUEMAS_ENLACE)} (el portal lo muestra como texto)",
                        entidad=entidad.ref.corta,
                        origen=entidad.origen,
                    )
                )
        for dueno in catalogo.refs(entidad, "owner"):
            if dueno.kind not in {"group", "user"}:
                hallazgos.append(
                    Hallazgo(
                        "V16",
                        f"spec.owner debe ser un group o user, no {dueno.corta}",
                        entidad=entidad.ref.corta,
                        origen=entidad.origen,
                    )
                )

    configurados = {r.slug.lower() for r in catalogo.config.repos}
    for repo in catalogo.config.repos:
        # Si no se pudo leer su catalog-info.yaml, el componente puede estar ahí.
        if repo.ignorar or descriptor_sin_leer(catalogo, repo.slug):
            continue
        if not catalogo.componentes_de_repo(repo.slug):
            hallazgos.append(
                Hallazgo(
                    "V16",
                    "repo sin componente en el catálogo (agregar su catalog-info.yaml o "
                    "marcarlo con ignorar: true y un motivo)",
                    repo=repo.slug,
                    origen="py-backstage.yaml",
                )
            )
    for componente in catalogo.de_tipo("component"):
        slug = (componente.anotacion(ANOT_GITHUB) or "").lower()
        if slug and slug not in configurados:
            hallazgos.append(
                Hallazgo(
                    "V16",
                    f"su repo {slug} no está en py-backstage.yaml: no se puede auditar",
                    entidad=componente.ref.corta,
                    origen=componente.origen,
                )
            )
    for repo in catalogo.config.repos:
        if repo.ignorar and not (repo.motivo or "").strip():
            hallazgos.append(
                Hallazgo(
                    "V16", "ignorar: true sin motivo", repo=repo.slug, origen="py-backstage.yaml"
                )
            )
    return hallazgos


# ── V15 / V17 ───────────────────────────────────────────────────────────────


def _prefijo(componente: Entidad) -> str:
    """``py-backstage/ruta`` normalizada (``""`` si no tiene o es la raíz)."""
    crudo = (componente.anotacion(ANOT_RUTA) or "").strip("/")
    if not crudo:
        return ""
    prefijo = posixpath.normpath(crudo)
    return "" if prefijo == "." else prefijo


def _bajo(ruta: str, prefijo: str) -> bool:
    return ruta == prefijo or ruta.startswith(prefijo + "/")


def _responsables(componentes: list[Entidad], ruta: str) -> list[Entidad]:
    """Componentes de un repo a los que se atribuye un archivo.

    Gana la ``py-backstage/ruta`` más específica que contiene el archivo
    (``apps/api/admin`` antes que ``apps/api``); si ninguna lo contiene, los
    componentes sin ruta, y si no hay, todos.
    """
    sin_ruta = []
    mejores: list[Entidad] = []
    largo = -1
    for componente in componentes:
        prefijo = _prefijo(componente)
        if not prefijo:
            sin_ruta.append(componente)
        elif _bajo(ruta, prefijo):
            if len(prefijo) > largo:
                mejores, largo = [componente], len(prefijo)
            elif len(prefijo) == largo:
                mejores.append(componente)
    return mejores or sin_ruta or componentes


def _evidencias(repo: str, coincidencias: list[Coincidencia]) -> list[Evidencia]:
    return [Evidencia(repo, c.ruta, c.linea, c.extracto) for c in coincidencias]


def auditar(
    catalogo: Catalogo,
    *,
    repos: list[str] | None = None,
    reglas: list[str] | None = None,
    incluir_pruebas: bool = False,
    incluir_docs: bool = False,
) -> Resultado:
    reglas = [r.upper() for r in (reglas or list(REGLAS))]
    filtro = {r.lower() for r in repos} if repos else None
    resultado = Resultado()

    if "V16" in reglas:
        hallazgos_v16 = revisar_metadatos(catalogo)
        if filtro is not None:
            hallazgos_v16 = [
                h
                for h in hallazgos_v16
                if (h.repo or "").lower() in filtro or _de_repo(catalogo, h.entidad, filtro)
            ]
        resultado.hallazgos += hallazgos_v16

    huellas = compilar(_objetivos(catalogo))

    # V16 también lee los repos: revisa que cada py-backstage/ruta tenga archivos.
    if {"V15", "V16", "V17"} & set(reglas):
        for repo_config in catalogo.config.repos:
            if repo_config.ignorar or (
                filtro is not None and repo_config.slug.lower() not in filtro
            ):
                continue
            repo = Repo(
                repo_config.slug,
                catalogo.config.ruta_de(repo_config, catalogo.base),
                repo_config.rama,
            )
            _auditar_repo(
                catalogo,
                repo,
                repo_config.excluir,
                huellas,
                reglas,
                resultado,
                incluir_pruebas=incluir_pruebas,
                incluir_docs=incluir_docs,
            )
        if filtro is not None:
            desconocidos = filtro - {r.slug.lower() for r in catalogo.config.repos}
            for slug in sorted(desconocidos):
                resultado.no_auditados[slug] = "no está en py-backstage.yaml"

    resultado.hallazgos.sort(
        key=lambda h: (h.regla, h.repo or "", h.entidad or "", h.objetivo or "")
    )
    return resultado


def _de_repo(catalogo: Catalogo, entidad: str | None, filtro: set[str]) -> bool:
    if not entidad:
        return False
    for e in catalogo.entidades.values():
        if e.ref.corta == entidad:
            return (e.anotacion(ANOT_GITHUB) or "").lower() in filtro
    return False


def _auditar_repo(
    catalogo: Catalogo,
    repo: Repo,
    excluir: list[str],
    huellas: Huellas,
    reglas: list[str],
    resultado: Resultado,
    *,
    incluir_pruebas: bool,
    incluir_docs: bool,
) -> None:
    if not repo.disponible:
        resultado.no_auditados[repo.slug] = f"sin clon local en {repo.ruta}"
        return
    try:
        referencia = repo.referencia()
        sha, fecha = repo.commit(referencia)
        archivos = repo.archivos(referencia)
    except ErrorGit as exc:
        resultado.no_auditados[repo.slug] = str(exc)
        return

    componentes = catalogo.componentes_de_repo(repo.slug)
    if "V16" in reglas:
        for componente in componentes:
            prefijo = _prefijo(componente)
            if prefijo and not any(_bajo(a.ruta, prefijo) for a in archivos):
                resultado.hallazgos.append(
                    Hallazgo(
                        "V16",
                        f"{ANOT_RUTA} {prefijo!r} no tiene archivos en {referencia}",
                        entidad=componente.ref.corta,
                        repo=repo.slug,
                        origen=componente.origen,
                    )
                )
    if not {"V15", "V17"} & set(reglas):
        resultado.auditados.append(RepoAuditado(repo.slug, referencia, sha, fecha, len(archivos)))
        return
    patrones = list(excluir)
    for componente in componentes:
        patrones += componente.lista_anotacion(ANOT_EXCLUIR)
    candidatos = candidatos_evidencia(
        archivos, patrones, incluir_pruebas=incluir_pruebas, incluir_docs=incluir_docs
    )
    resultado.auditados.append(RepoAuditado(repo.slug, referencia, sha, fecha, len(candidatos)))

    # objetivo -> [(coincidencia, componentes a los que se atribuye el archivo)]
    uso: dict[Ref, list[tuple[Coincidencia, tuple[Ref, ...]]]] = defaultdict(list)
    try:
        for archivo, texto in repo.leer(candidatos):
            responsables = tuple(
                c.ref for c in (_responsables(componentes, archivo.ruta) if componentes else [])
            )
            for destino, coincidencias in buscar(huellas, archivo.ruta, texto).items():
                uso[destino] += [(c, responsables) for c in coincidencias]
    except ErrorGit as exc:
        resultado.no_auditados[repo.slug] = str(exc)
        return

    if "V15" in reglas:
        for objetivo_ref in sorted(uso):
            objetivo = catalogo.obtener(objetivo_ref)
            if objetivo is None:
                continue
            exentos = catalogo.proveedores(objetivo) | {objetivo_ref}
            declarantes = exentos | {
                c.ref for c in componentes if objetivo_ref in catalogo.dependencias(c)
            }
            # Un archivo compartido por varios componentes (mismo código, varios
            # servicios) queda cubierto si UNO de ellos declara el uso.
            sueltas = [
                (c, quienes) for c, quienes in uso[objetivo_ref] if not set(quienes) & declarantes
            ]
            if not sueltas:
                continue
            implicados = sorted({r for _, quienes in sueltas for r in quienes})
            quien = ", ".join(r.corta for r in implicados) or "el repo (sin componente)"
            campo = "consumesApis" if objetivo_ref.kind == "api" else "dependsOn"
            coincidencias = [c for c, _ in sueltas]
            resultado.hallazgos.append(
                Hallazgo(
                    "V15",
                    f"{quien} usa {objetivo_ref.corta} y no lo declara en spec.{campo}",
                    entidad=", ".join(r.corta for r in implicados) or None,
                    objetivo=objetivo_ref.corta,
                    repo=repo.slug,
                    evidencias=_evidencias(repo.slug, coincidencias[:MAX_EVIDENCIAS]),
                    total_evidencias=len(coincidencias),
                )
            )

    if "V17" in reglas:
        verificables = huellas.objetivos
        for componente in componentes:
            for dependencia in catalogo.dependencias(componente):
                if dependencia not in catalogo.entidades:
                    continue
                if dependencia not in verificables:
                    resultado.sin_huellas.append((componente.ref.corta, dependencia.corta))
                    continue
                if any(
                    componente.ref in quienes
                    for c, quienes in uso.get(dependencia, [])
                    if not es_plantilla_env(c.ruta)
                ):
                    continue
                resultado.hallazgos.append(
                    Hallazgo(
                        "V17",
                        f"{componente.ref.corta} declara {dependencia.corta} y su código "
                        "no muestra ninguna huella",
                        entidad=componente.ref.corta,
                        objetivo=dependencia.corta,
                        repo=repo.slug,
                        origen=componente.origen,
                    )
                )
