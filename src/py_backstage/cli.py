"""Línea de comandos de py-backstage.

Sólo lectura (sirven con permisos de lectura):
    validar, auditar, grafo, validar-descriptor

Escriben:
    sitio, nuevo-servicio, auditar --actualizar

Red (GET con el ``gh`` local):
    descubrir

Códigos de salida: 0 sin hallazgos, 1 con hallazgos, 2 error de uso o de catálogo.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .auditoria import REGLAS, Hallazgo, Resultado, auditar, revisar_metadatos
from .catalogo import VARIABLE_CATALOGO, Catalogo, ErrorCatalogo, cargar, parsear_entidades
from .modelo import Ref, parsear_ref, sin_control
from .repos import ErrorGit, Repo


def _imprimir_json(datos: Any) -> None:
    json.dump(datos, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _texto_hallazgo(h: Hallazgo) -> list[str]:
    donde = " · ".join(x for x in (h.repo, h.origen) if x)
    lineas = [f"[{h.regla} {h.severidad}] {h.mensaje}" + (f"  ({donde})" if donde else "")]
    for e in h.evidencias:
        lineas.append(f"    {e.archivo}:{e.linea}  {e.extracto}")
    # Mensajes y extractos traen texto de los repos auditados.
    lineas = [sin_control(linea) for linea in lineas]
    if h.total_evidencias > len(h.evidencias):
        lineas.append(f"    … y {h.total_evidencias - len(h.evidencias)} coincidencias más")
    return lineas


def _imprimir_resultado(resultado: Resultado) -> None:
    for r in resultado.auditados:
        print(f"auditado {r.slug} en {r.referencia} ({r.commit}, {r.fecha}): {r.archivos} archivos")
    for slug, motivo in sorted(resultado.no_auditados.items()):
        print(f"NO AUDITADO {slug}: {motivo}")
    if resultado.auditados or resultado.no_auditados:
        print()
    for h in resultado.hallazgos:
        print("\n".join(_texto_hallazgo(h)))
    conteo = {regla: 0 for regla in REGLAS}
    for h in resultado.hallazgos:
        conteo[h.regla] += 1
    if resultado.sin_huellas:
        print(
            f"\n{len(resultado.sin_huellas)} dependencias declaradas hacia entidades sin huellas "
            "(no verificables): " + ", ".join(f"{e}→{o}" for e, o in resultado.sin_huellas)
        )
    print("\n" + " · ".join(f"{regla}: {n}" for regla, n in conteo.items()))


def _cargar(args: argparse.Namespace, leer_repos: bool = True) -> Catalogo:
    return cargar(args.catalogo, leer_repos=leer_repos)


# ── comandos ────────────────────────────────────────────────────────────────


def cmd_validar(args: argparse.Namespace) -> int:
    catalogo = _cargar(args, leer_repos=not args.sin_repos)
    hallazgos = revisar_metadatos(catalogo)
    if args.formato == "json":
        _imprimir_json(
            {
                "entidades": len(catalogo.entidades),
                "hallazgos": [h.como_dict() for h in hallazgos],
                "descriptores_leidos": catalogo.leidos,
                "no_leidos": catalogo.no_leidos,
            }
        )
    else:
        print(f"{len(catalogo.entidades)} entidades")
        for slug, motivo in sorted(catalogo.no_leidos.items()):
            print(f"descriptor no leído {slug}: {motivo}")
        for h in hallazgos:
            print("\n".join(_texto_hallazgo(h)))
        print(f"V16: {len(hallazgos)}")
    return 1 if hallazgos else 0


def cmd_auditar(args: argparse.Namespace) -> int:
    catalogo = _cargar(args)
    if args.actualizar:
        for repo_config in catalogo.config.repos:
            if repo_config.ignorar:
                continue
            repo = Repo(
                repo_config.slug,
                catalogo.config.ruta_de(repo_config, catalogo.base),
                repo_config.rama,
            )
            if args.repo and repo.slug.lower() not in {r.lower() for r in args.repo}:
                continue
            if repo.disponible:
                try:
                    repo.actualizar()
                except ErrorGit as exc:
                    print(f"no se pudo actualizar {repo.slug}: {exc}", file=sys.stderr)
        catalogo = _cargar(args)
    resultado = auditar(
        catalogo,
        repos=args.repo,
        reglas=args.regla,
        incluir_pruebas=args.incluir_pruebas,
        incluir_docs=args.incluir_docs,
    )
    if args.formato == "json":
        _imprimir_json(resultado.como_dict())
    else:
        _imprimir_resultado(resultado)
    return 1 if resultado.hallazgos else 0


def cmd_grafo(args: argparse.Namespace) -> int:
    from .sitio import grafo_mermaid

    catalogo = _cargar(args, leer_repos=not args.sin_repos)
    sistema: Ref | None = None
    if args.sistema:
        sistema = parsear_ref(args.sistema, "system")
        if sistema not in catalogo.entidades:
            print(f"{sistema.corta} no existe en el catálogo", file=sys.stderr)
            return 2
    print(grafo_mermaid(catalogo, sistema))
    return 0


def cmd_validar_descriptor(args: argparse.Namespace) -> int:
    """Valida archivos sueltos (el CI de cada repo); no resuelve referencias."""
    problemas: list[str] = []
    for archivo in args.archivos:
        ruta = Path(archivo)
        if not ruta.is_file():
            print(f"{archivo}: no existe", file=sys.stderr)
            return 2
        entidades, errores = parsear_entidades(ruta.read_text(encoding="utf-8"), archivo)
        problemas += [str(e) for e in errores]
        for entidad in entidades:
            if not (entidad.metadata.description or "").strip() and entidad.kind != "Location":
                problemas.append(f"{archivo} [{entidad.ref.corta}]: falta metadata.description")
            if entidad.kind in {"Component", "API", "Resource"} and not entidad.spec.get("system"):
                problemas.append(f"{archivo} [{entidad.ref.corta}]: falta spec.system")
        if not entidades and not errores:
            problemas.append(f"{archivo}: no tiene entidades")
    for p in problemas:
        print(p)
    print(f"{len(args.archivos)} archivos, {len(problemas)} problemas")
    return 1 if problemas else 0


def cmd_sitio(args: argparse.Namespace) -> int:
    from .sitio import ErrorSitio, generar

    catalogo = _cargar(args)
    resultado = auditar(catalogo) if args.con_auditoria else None
    try:
        escritos = generar(catalogo, Path(args.salida), titulo=args.titulo, resultado=resultado)
    except ErrorSitio as exc:
        print(exc, file=sys.stderr)
        return 2
    print(
        f"{len(escritos)} archivos en {args.salida}; vista previa: mkdocs serve -f "
        f"{Path(args.salida) / 'mkdocs.yml'}"
    )
    return 0


def cmd_nuevo(args: argparse.Namespace) -> int:
    from .nuevo import ErrorNuevo, Pedido, aviso_plantilla_sucia, crear, plantilla_local, preparar

    # Con los descriptores de los repos: el sistema o la dependencia puede vivir
    # en el catalog-info.yaml de otro repo.
    catalogo = _cargar(args) if args.catalogo or _hay_variable() else None
    if catalogo is None:
        print(
            "aviso: sin catálogo, no se validó que existan el sistema, el dueño ni las "
            "dependencias (usar --catalogo)",
            file=sys.stderr,
        )
    pedido = Pedido(
        nombre=args.nombre,
        descripcion=args.descripcion,
        sistema=args.sistema,
        dueno=args.dueno,
        org=args.org or (catalogo.config.org if catalogo and catalogo.config.org else ""),
        dependencias=args.depende_de or [],
        usa_postgres=args.postgres,
    )
    if not pedido.org:
        print("falta --org (o org: en py-backstage.yaml)", file=sys.stderr)
        return 2
    try:
        datos = preparar(catalogo, pedido)
        plantilla = args.plantilla
        if plantilla == "local":
            local = plantilla_local()
            if local is None:
                print("no se encontró la plantilla local", file=sys.stderr)
                return 2
            plantilla = str(local)
        aviso = aviso_plantilla_sucia(plantilla, args.ref)
        if aviso:
            print(aviso, file=sys.stderr)
        destino = Path(args.destino)
        fuente = crear(destino, datos, plantilla, args.ref)
    except ErrorNuevo as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"creado {destino} desde {fuente}")
    print("Para que el catálogo lo audite, agregar a py-backstage.yaml:")
    print(f"  - slug: {pedido.org}/{pedido.nombre}")
    return 0


def cmd_descubrir(args: argparse.Namespace) -> int:
    from .descubrir import ErrorDescubrir, listar, no_catalogados

    catalogo = _cargar(args, leer_repos=False)
    org = args.org or catalogo.config.org
    if not org:
        print("falta --org (o org: en py-backstage.yaml)", file=sys.stderr)
        return 2
    try:
        hallazgos = no_catalogados(catalogo, listar(org), args.incluir_archivados)
    except ErrorDescubrir as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.formato == "json":
        _imprimir_json([h.como_dict() for h in hallazgos])
    else:
        for h in hallazgos:
            print("\n".join(_texto_hallazgo(h)))
        print(f"V16: {len(hallazgos)}")
    return 1 if hallazgos else 0


def _hay_variable() -> bool:
    import os

    return bool(os.environ.get(VARIABLE_CATALOGO))


# ── argparse ────────────────────────────────────────────────────────────────


def construir_parser() -> argparse.ArgumentParser:
    # Sin abreviaturas: '--actual' no debe valer por '--actualizar', así un
    # permiso que sólo deja correr los comandos de lectura no se esquiva.
    parser = argparse.ArgumentParser(
        prog="py-backstage",
        description="Catálogo de software con el formato de Backstage.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"py-backstage {__version__}")
    sub = parser.add_subparsers(dest="comando", required=True)

    def nuevo_comando(nombre: str, ayuda: str) -> argparse.ArgumentParser:
        return sub.add_parser(nombre, help=ayuda, allow_abbrev=False)

    def con_catalogo(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--catalogo",
            help=f"carpeta o git:<repo>@<ref>:<carpeta> (por defecto ${VARIABLE_CATALOGO})",
        )

    def con_formato(p: argparse.ArgumentParser) -> None:
        p.add_argument("--formato", choices=["texto", "json"], default="texto")

    p = nuevo_comando("validar", "formato, referencias y metadatos (V16)")
    con_catalogo(p)
    con_formato(p)
    p.add_argument(
        "--sin-repos", action="store_true", help="no leer catalog-info.yaml de los repos"
    )
    p.set_defaults(func=cmd_validar)

    p = nuevo_comando("auditar", "V15, V16 y V17 contra el código en origin/<rama>")
    con_catalogo(p)
    con_formato(p)
    p.add_argument("--repo", action="append", help="sólo este repo (org/nombre); repetible")
    p.add_argument("--regla", action="append", choices=list(REGLAS), help="sólo esta regla")
    p.add_argument("--incluir-pruebas", action="store_true", help="contar tests como evidencia")
    p.add_argument("--incluir-docs", action="store_true", help="contar docs como evidencia")
    p.add_argument(
        "--actualizar", action="store_true", help="git fetch de cada repo antes (escribe refs)"
    )
    p.set_defaults(func=cmd_auditar)

    p = nuevo_comando("grafo", "grafo Mermaid de dependencias")
    con_catalogo(p)
    p.add_argument("--sistema", help="sólo este sistema y sus vecinos")
    p.add_argument("--sin-repos", action="store_true")
    p.set_defaults(func=cmd_grafo)

    p = nuevo_comando("validar-descriptor", "valida catalog-info.yaml sueltos (CI)")
    p.add_argument("archivos", nargs="+")
    p.set_defaults(func=cmd_validar_descriptor)

    p = nuevo_comando("sitio", "genera el portal MkDocs")
    con_catalogo(p)
    p.add_argument("--salida", required=True)
    p.add_argument("--titulo", default="Catálogo")
    p.add_argument("--con-auditoria", action="store_true", help="incluye la última auditoría")
    p.set_defaults(func=cmd_sitio)

    p = nuevo_comando("nuevo-servicio", "crea un servicio desde la plantilla")
    con_catalogo(p)
    p.add_argument("destino")
    p.add_argument("--nombre", required=True)
    p.add_argument("--descripcion", required=True)
    p.add_argument("--sistema", required=True)
    p.add_argument("--dueno", required=True)
    p.add_argument("--org")
    p.add_argument("--depende-de", action="append", help="api:x, resource:y…; repetible")
    p.add_argument("--postgres", action="store_true", help="incluye Postgres en código y CI")
    p.add_argument(
        "--plantilla",
        help="fuente Copier (URL git o carpeta; 'local' = este checkout). Por defecto, la pública",
    )
    p.add_argument(
        "--ref",
        help="tag o commit de la plantilla (por defecto: el de esta versión; 'local' usa HEAD)",
    )
    p.set_defaults(func=cmd_nuevo)

    p = nuevo_comando("descubrir", "repos de la organización fuera del catálogo (gh)")
    con_catalogo(p)
    con_formato(p)
    p.add_argument("--org")
    p.add_argument("--incluir-archivados", action="store_true")
    p.set_defaults(func=cmd_descubrir)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = construir_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ErrorCatalogo as exc:
        print(f"catálogo: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
