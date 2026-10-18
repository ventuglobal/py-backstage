"""Portal del catálogo: un proyecto MkDocs (Material) generado desde las entidades.

Cumple el papel del portal de Backstage: una página por sistema y por entidad
(dueño, repo, ciclo de vida, dependencias en ambos sentidos), grafos Mermaid y,
si se pide, el último resultado de la auditoría.
"""

from __future__ import annotations

import html
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path

from .auditoria import REGLAS, Resultado
from .catalogo import Catalogo, relaciones
from .modelo import ANOT_GITHUB, ANOT_RUTA, SLUG_GITHUB, Entidad, Ref, enlace_seguro

MARCA = ".py-backstage-sitio"
ETIQUETA_RELACION = {
    "dependsOn": "depende de",
    "consumesApis": "consume",
    "providesApis": "provee",
    "subcomponentOf": "es parte de",
}
FORMA = {
    "component": ('["', '"]'),
    "api": ('{{"', '"}}'),
    "resource": ('[("', '")]'),
    "system": ('(["', '"])'),
}
TIPOS = [
    ("system", "Sistemas"),
    ("component", "Componentes"),
    ("api", "APIs"),
    ("resource", "Recursos"),
    ("group", "Grupos"),
    ("user", "Personas"),
    ("domain", "Dominios"),
]

MKDOCS = """\
# Generado por py-backstage: no editar a mano.
site_name: {titulo}
docs_dir: docs
theme:
  name: material
  language: es
  features:
    - navigation.sections
    - navigation.indexes
    - search.suggest
markdown_extensions:
  - admonition
  - attr_list
  - tables
  - toc:
      permalink: true
  - pymdownx.superfences:
      custom_fences:
        - name: mermaid
          class: mermaid
          format: !!python/name:pymdownx.superfences.fence_code_format
plugins:
  - search:
      lang: es
"""


class ErrorSitio(RuntimeError):
    pass


# Escapes de prefijo único ('_' y una letra): dos referencias distintas nunca dan
# el mismo id (con '_' a secas, 'a-b' y 'a.b' chocaban).
_ESCAPES_ID = {"-": "_d", ".": "_p", "_": "_u", ":": "_c", "/": "_s"}


def _id(ref: Ref) -> str:
    """Id de nodo Mermaid legible y distinto para cada referencia."""
    return "".join(
        c if c.isascii() and c.isalnum() else _ESCAPES_ID.get(c, f"_x{ord(c):04x}")
        for c in ref.corta
    )


# Caracteres que Markdown (y la extensión de tablas) interpretaría.
_MARCAS_MD = re.compile(r"([\\`*_{}\[\]()#+\-.!|])")
# Entidades de Mermaid: '#' primero, porque las demás lo usan.
_MERMAID = (("#", "#35;"), ('"', "#quot;"), ("<", "#lt;"), (">", "#gt;"), ("`", "#96;"))


def _texto(texto: str | None) -> str:
    """Texto plano en una línea: sin HTML, enlaces ni formato Markdown.

    Descripciones, títulos y mensajes vienen de los descriptores de cada repo:
    un ``<script>`` o un ``[x](javascript:…)`` no deben llegar al portal.
    """
    plano = " ".join((texto or "").split())
    return _MARCAS_MD.sub(r"\\\1", html.escape(plano, quote=False))


def _codigo(texto: str | None) -> str:
    """Código en línea con una cerca más larga que cualquier tilde invertida del texto."""
    plano = " ".join((texto or "").split())
    if not plano:
        return ""
    cerca = "`" * (max((len(t) for t in re.findall(r"`+", plano)), default=0) + 1)
    return f"{cerca} {plano} {cerca}"


def _bloque(texto: str) -> list[str]:
    """Bloque de código cuya cerca no puede cerrarse desde adentro."""
    cerca = "`" * max(3, max((len(t) for t in re.findall(r"`+", texto)), default=0) + 1)
    return [cerca, texto, cerca]


def _etiqueta_nodo(texto: str) -> str:
    plano = " ".join(texto.split())
    for caracter, entidad in _MERMAID:
        plano = plano.replace(caracter, entidad)
    return plano


def _pagina(ref: Ref) -> str:
    # El namespace no lleva '/', así que la carpeta no puede chocar con un nombre.
    return f"entidades/{ref.kind}/{ref.namespace}/{ref.name}.md"


def _enlace(ref: Ref, desde: str, catalogo: Catalogo) -> str:
    entidad = catalogo.obtener(ref)
    titulo = (entidad.metadata.title if entidad else None) or ref.corta
    if entidad is None:
        return _codigo(ref.corta)
    profundidad = desde.count("/")
    return f"[{_texto(titulo)}]({'../' * profundidad}{_pagina(ref)})"


def grafo_mermaid(catalogo: Catalogo, sistema: Ref | None = None) -> str:
    """Grafo de dependencias (todo el catálogo o un sistema y sus vecinos)."""
    aristas = relaciones(catalogo)
    tipos = {"component", "api", "resource"}
    nodos = {r for r, e in catalogo.entidades.items() if r.kind in tipos}
    if sistema is not None:
        propios = {r for r in nodos if sistema in catalogo.refs(catalogo.entidades[r], "system")}
        vecinos = {d for r in propios for _, d in aristas.get(r, ())}
        vecinos |= {r for r, salientes in aristas.items() for _, d in salientes if d in propios}
        nodos = (propios | vecinos) & nodos
    por_sistema: dict[Ref | None, list[Ref]] = defaultdict(list)
    for ref in sorted(nodos):
        sistemas = catalogo.refs(catalogo.entidades[ref], "system")
        por_sistema[sistemas[0] if sistemas else None].append(ref)

    lineas = ["flowchart LR"]
    for sis in sorted(por_sistema, key=lambda r: (r is None, str(r))):
        refs = por_sistema[sis]
        sangria = "    "
        if sis is not None:
            entidad = catalogo.obtener(sis)
            titulo = (entidad.metadata.title if entidad else None) or sis.name
            lineas.append(f'    subgraph {_id(sis)}["{_etiqueta_nodo(titulo)}"]')
            sangria = "        "
        for ref in refs:
            entidad = catalogo.entidades[ref]
            abre, cierra = FORMA[ref.kind]
            texto = _etiqueta_nodo(entidad.metadata.title or ref.name)
            lineas.append(f"{sangria}{_id(ref)}{abre}{texto}{cierra}")
        if sis is not None:
            lineas.append("    end")
    for origen in sorted(nodos):
        for campo, destino in sorted(aristas.get(origen, ())):
            if destino not in nodos:
                continue
            if campo == "providesApis":
                lineas.append(f"    {_id(origen)} ==>|provee| {_id(destino)}")
            elif campo == "consumesApis":
                lineas.append(f"    {_id(origen)} -.->|consume| {_id(destino)}")
            elif campo == "subcomponentOf":
                lineas.append(f"    {_id(origen)} --- {_id(destino)}")
            else:
                lineas.append(f"    {_id(origen)} --> {_id(destino)}")
    return "\n".join(lineas)


def _ficha(entidad: Entidad, catalogo: Catalogo, entrantes: dict[Ref, set[tuple[str, Ref]]]) -> str:
    ref = entidad.ref
    desde = _pagina(ref)
    m = entidad.metadata
    partes = [f"# {_texto(m.title or m.name)}", ""]
    if m.description and m.description.strip():
        partes += [_texto(m.description), ""]
    filas = [("Referencia", _codigo(ref.corta)), ("Tipo", entidad.kind)]
    for campo, etiqueta in (("type", "Clase"), ("lifecycle", "Ciclo de vida")):
        if entidad.spec.get(campo):
            filas.append((etiqueta, _codigo(str(entidad.spec[campo]))))
    for campo, etiqueta in (("owner", "Dueño"), ("system", "Sistema"), ("domain", "Dominio")):
        for destino in catalogo.refs(entidad, campo):
            filas.append((etiqueta, _enlace(destino, desde, catalogo)))
    slug = entidad.anotacion(ANOT_GITHUB)
    if slug:
        ruta = entidad.anotacion(ANOT_RUTA)
        sufijo = f" ({_codigo(ruta)})" if ruta else ""
        if SLUG_GITHUB.fullmatch(slug):
            repo = f"[{_texto(slug)}](https://github.com/{slug})"
        else:
            repo = _codigo(slug)
        filas.append(("Repo", repo + sufijo))
    if m.tags:
        filas.append(("Etiquetas", " ".join(_codigo(t) for t in m.tags)))
    partes += ["| | |", "|---|---|"] + [f"| {a} | {b} |" for a, b in filas] + [""]

    if m.links:
        partes += ["## Enlaces", ""]
        for e in m.links:
            url = enlace_seguro(e.url)
            if url is None:
                visible = [p for p in (_texto(e.title), _codigo(e.url)) if p]
                partes.append("- " + " ".join(visible))
            else:
                partes.append(f"- [{_texto(e.title or e.url)}]({url})")
        partes.append("")

    salientes = relaciones(catalogo).get(ref, set())
    if salientes:
        partes += ["## Usa", ""]
        for campo, destino in sorted(salientes):
            partes.append(f"- {ETIQUETA_RELACION[campo]} {_enlace(destino, desde, catalogo)}")
        partes.append("")
    if entrantes.get(ref):
        partes += ["## La usan", ""]
        for campo, origen in sorted(entrantes[ref]):
            partes.append(f"- {_enlace(origen, desde, catalogo)} ({ETIQUETA_RELACION[campo]})")
        partes.append("")
    if entidad.kind == "API" and entidad.spec.get("definition"):
        partes += ["## Definición", "", *_bloque(str(entidad.spec["definition"]).rstrip()), ""]
    partes += ["---", f"Fuente: {_codigo(entidad.origen)}", ""]
    return "\n".join(partes)


def _auditoria(resultado: Resultado) -> str:
    partes = ["# Auditoría del catálogo", ""]
    if resultado.auditados:
        partes += ["## Repos auditados", "", "| Repo | Rama | Commit | Fecha | Archivos |"]
        partes.append("|---|---|---|---|---|")
        for r in resultado.auditados:
            partes.append(
                f"| {_texto(r.slug)} | {_codigo(r.referencia)} | {_codigo(r.commit)} | "
                f"{_texto(r.fecha)} | {r.archivos} |"
            )
        partes.append("")
    if resultado.no_auditados:
        partes += ["## No auditados", ""]
        partes += [
            f"- **{_texto(s)}**: {_texto(m)}" for s, m in sorted(resultado.no_auditados.items())
        ]
        partes.append("")
    for regla, (severidad, titulo) in REGLAS.items():
        propios = [h for h in resultado.hallazgos if h.regla == regla]
        partes += [f"## {regla} · {titulo} ({severidad}) — {len(propios)}", ""]
        for h in propios:
            partes.append(f"- {_texto(h.mensaje)}" + (f" — {_codigo(h.repo)}" if h.repo else ""))
            for e in h.evidencias:
                partes.append(f"    - {_codigo(f'{e.archivo}:{e.linea}')} {_codigo(e.extracto)}")
            if h.total_evidencias > len(h.evidencias):
                partes.append(f"    - … y {h.total_evidencias - len(h.evidencias)} más")
        partes.append("")
    return "\n".join(partes)


def generar(
    catalogo: Catalogo,
    salida: Path,
    *,
    titulo: str = "Catálogo",
    resultado: Resultado | None = None,
) -> list[Path]:
    """Escribe el proyecto MkDocs en ``salida`` y devuelve los archivos creados."""
    salida = salida.resolve()
    if salida.exists() and not salida.is_dir():
        raise ErrorSitio(f"{salida} no es una carpeta")
    if salida.exists() and any(salida.iterdir()) and not (salida / MARCA).exists():
        raise ErrorSitio(f"{salida} no está vacía y no la generó py-backstage: no la toco")
    docs = salida / "docs"
    if docs.exists():
        shutil.rmtree(docs)
    docs.mkdir(parents=True)
    (salida / MARCA).write_text("Carpeta generada por py-backstage sitio.\n", encoding="utf-8")
    escritos = [salida / "mkdocs.yml"]
    # Un string JSON es un escalar YAML válido: ':' o '#' en el título no lo rompen.
    # Sin ensure_ascii: un emoji escrito como par sustituto (\ud83d\ude80) rompe mkdocs.
    titulo_yaml = json.dumps(titulo, ensure_ascii=False)
    (salida / "mkdocs.yml").write_text(MKDOCS.format(titulo=titulo_yaml), encoding="utf-8")

    entrantes: dict[Ref, set[tuple[str, Ref]]] = defaultdict(set)
    for origen, salientes in relaciones(catalogo).items():
        for campo, destino in salientes:
            entrantes[destino].add((campo, origen))

    def escribir(relativa: str, texto: str) -> None:
        ruta = docs / relativa
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(texto, encoding="utf-8")
        escritos.append(ruta)

    indice = [f"# {_texto(titulo)}", "", f"{len(catalogo.entidades)} entidades.", ""]
    indice += ["- [Grafo de dependencias](grafo.md)"]
    if resultado is not None:
        abiertos = len(resultado.hallazgos)
        indice += [f"- [Auditoría](auditoria.md): {abiertos} hallazgos"]
    indice.append("")
    for kind, nombre in TIPOS:
        entidades = catalogo.de_tipo(kind)
        if not entidades:
            continue
        indice += [f"## {nombre}", "", "| Nombre | Descripción | Dueño |", "|---|---|---|"]
        for e in entidades:
            duenos = ", ".join(_enlace(d, "index.md", catalogo) for d in catalogo.refs(e, "owner"))
            indice.append(
                f"| {_enlace(e.ref, 'index.md', catalogo)} | {_texto(e.metadata.description)} | "
                f"{duenos} |"
            )
        indice.append("")
    escribir("index.md", "\n".join(indice))
    escribir(
        "grafo.md",
        "# Grafo de dependencias\n\n```mermaid\n" + grafo_mermaid(catalogo) + "\n```\n",
    )
    for entidad in catalogo.entidades.values():
        if entidad.kind == "Location":
            continue
        texto = _ficha(entidad, catalogo, entrantes)
        if entidad.kind == "System":
            texto += "\n## Grafo\n\n```mermaid\n" + grafo_mermaid(catalogo, entidad.ref) + "\n```\n"
            miembros = [
                e for e in catalogo.entidades.values() if entidad.ref in catalogo.refs(e, "system")
            ]
            if miembros:
                texto += (
                    "\n## Partes\n\n"
                    + "\n".join(
                        f"- {_enlace(e.ref, _pagina(entidad.ref), catalogo)} ({e.kind})"
                        for e in sorted(miembros, key=lambda e: e.ref)
                    )
                    + "\n"
                )
        escribir(_pagina(entidad.ref), texto)
    if resultado is not None:
        escribir("auditoria.md", _auditoria(resultado))
    return escritos
