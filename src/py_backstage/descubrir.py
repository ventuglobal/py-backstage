"""``descubrir``: repos de la organización que el catálogo no conoce (sólo lectura).

Usa ``gh repo list`` (una consulta GET con la sesión del ``gh`` local). Un repo
nuevo que nadie agregó a ``py-backstage.yaml`` es justamente el proyecto que
nace desacoplado: se informa como V16.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from .auditoria import Hallazgo
from .catalogo import Catalogo


class ErrorDescubrir(RuntimeError):
    pass


@dataclass
class RepoRemoto:
    slug: str
    archivado: bool
    fork: bool
    visibilidad: str


def listar(org: str, limite: int = 500) -> list[RepoRemoto]:
    campos = "nameWithOwner,isArchived,isFork,visibility"
    try:
        salida = subprocess.run(
            ["gh", "repo", "list", org, "--limit", str(limite), "--json", campos],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ErrorDescubrir(f"gh repo list falló: {exc}") from exc
    if salida.returncode != 0:
        raise ErrorDescubrir(f"gh repo list: {salida.stderr.strip() or 'error'}")
    return [
        RepoRemoto(r["nameWithOwner"], r["isArchived"], r["isFork"], str(r["visibility"]).lower())
        for r in json.loads(salida.stdout or "[]")
    ]


def no_catalogados(
    catalogo: Catalogo, remotos: list[RepoRemoto], incluir_archivados: bool = False
) -> list[Hallazgo]:
    conocidos = {r.slug.lower() for r in catalogo.config.repos}
    hallazgos = []
    for repo in sorted(remotos, key=lambda r: r.slug.lower()):
        if repo.slug.lower() in conocidos or (repo.archivado and not incluir_archivados):
            continue
        detalle = ", ".join(
            x
            for x in (
                repo.visibilidad,
                "fork" if repo.fork else "",
                "archivado" if repo.archivado else "",
            )
            if x
        )
        hallazgos.append(
            Hallazgo(
                "V16",
                f"repo de la organización fuera del catálogo ({detalle}): agregarlo a "
                "py-backstage.yaml o marcarlo con ignorar: true y un motivo",
                repo=repo.slug,
                origen="gh repo list",
            )
        )
    return hallazgos
