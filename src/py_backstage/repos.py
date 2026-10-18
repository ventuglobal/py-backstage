"""Lectura de repos git sin tocar la copia de trabajo.

Todo se lee de los objetos de git en ``origin/<rama>`` (lo que está mergeado),
nunca de los archivos del disco: una copia local puede tener trabajo sin
commitear o estar en otra rama, y eso no debe cambiar el resultado de una
auditoría. Ninguna función de este módulo escribe en el repo salvo
``actualizar`` (``git fetch``), que sólo corre con ``auditar --actualizar``.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

# Archivos que no se leen: binarios conocidos, dependencias y artefactos.
EXTENSIONES_TEXTO = {
    ".py", ".pyi", ".rs", ".go", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts",
    ".vue", ".svelte", ".kt", ".kts", ".java", ".sql", ".sh", ".toml", ".yaml", ".yml", ".json",
    ".ini", ".cfg", ".example", ".graphql", ".gql", ".html", ".jinja", ".j2", ".tf", ".php",
    ".rb",
    # Documentación: sólo se lee con --incluir-docs (PATRONES_DOCS).
    ".md", ".rst", ".txt", ".adoc",
}  # fmt: skip
NOMBRES_TEXTO = {"Dockerfile", "Procfile", "Makefile", "railway.json", "nixpacks.toml"}
# De los .env sólo se leen las plantillas: un .env versionado puede traer secretos.
PLANTILLAS_ENV = {".env.example", ".env.sample", ".env.template", ".env.dist"}
SUFIJOS_PLANTILLA = (".example", ".sample", ".template", ".dist")
# Un patrón sin '/' se compara con el nombre del archivo en cualquier nivel; con
# '/', con la ruta desde la raíz del repo.
EXCLUIR_SIEMPRE = (
    ".git/*", "*/node_modules/*", "node_modules/*", "*/.venv/*", ".venv/*", "*/vendor/*",
    "vendor/*", "*/dist/*", "dist/*", "*/build/*", "build/*", "target/*", "*/target/*",
    "*.lock", "package-lock.json", "pnpm-lock.yaml", "*.min.js", "*.map",
    # Descriptores y respuestas de Copier: repiten las huellas del catálogo, no son uso.
    "catalog-info.yaml", ".copier-answers.yml",
)  # fmt: skip
# Pruebas y documentación: no cuentan como evidencia salvo que se pida.
PATRONES_PRUEBAS = (
    "tests/*", "*/tests/*", "test/*", "*/test/*", "__tests__/*", "*/__tests__/*", "spec/*",
    "*/spec/*", "e2e/*", "*/e2e/*", "*/src/test/*", "*/androidTest/*", "test_*.py", "*_test.py",
    "tests.py", "tests_*.py", "conftest.py", "*_test.go", "*.test.*", "*.spec.*", "*_spec.rb",
)  # fmt: skip
PATRONES_DOCS = ("docs/*", "*/docs/*", "*.md", "*.rst", "*.txt", "*.adoc", "CHANGELOG*", "LICENSE*")

TAMANO_MAXIMO = 512 * 1024
DESCRIPTOR = "catalog-info.yaml"


class ErrorGit(RuntimeError):
    pass


def _entorno() -> dict[str, str]:
    entorno = dict(os.environ)
    # Sin locks opcionales (git status no reescribe el índice), sin pager y sin
    # pedir credenciales: ninguna lectura debe colgarse ni escribir.
    # Tampoco se traen objetos que falten en un clon parcial.
    entorno.update(
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_PAGER="cat",
        GIT_NO_LAZY_FETCH="1",
        LC_ALL="C",
    )
    return entorno


# Configuración que podría ejecutar programas (verificar firmas, monitores,
# hooks) desactivada para todos los comandos.
_CONFIG_SEGURA = (
    "-c", "log.showSignature=false",
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
)  # fmt: skip
_ANCLA = "./"


def coincide(ruta: str, patrones: Iterable[str]) -> bool:
    """Si ``ruta`` calza algún patrón (ver ``normalizar_globs``)."""
    nombre = ruta.rsplit("/", 1)[-1]
    for patron in patrones:
        if patron.startswith(_ANCLA):
            if fnmatch.fnmatchcase(ruta, patron[len(_ANCLA) :]):
                return True
        elif fnmatch.fnmatchcase(ruta, patron) or (
            "/" not in patron and fnmatch.fnmatchcase(nombre, patron)
        ):
            return True
    return False


def _formas(patron: str, anclado: bool) -> list[str]:
    formas = [f"{_ANCLA}{patron}" if anclado and "/" not in patron else patron]
    if not patron.endswith("*"):
        # También todo lo que hay dentro, si es una carpeta.
        formas.append(f"{patron}/*")
        if not anclado:
            formas.append(f"*/{patron}/*")
    return formas


def normalizar_globs(patrones: Iterable[str]) -> list[str]:
    """Globs de ``excluir`` con la semántica habitual de ``.gitignore``.

    - sin ``/`` (``*.sql``, ``scripts``): el nombre, en cualquier nivel;
    - con ``/`` al inicio o en medio (``/seed.sql``, ``app/migraciones``): la
      ruta desde la raíz del repo;
    - ``dir``, ``dir/`` o ``dir/**``: la carpeta y todo lo que contiene;
    - ``**/x``: ``x`` en cualquier nivel, también en la raíz.

    ``*`` también cruza ``/`` (``fnmatch``), así que ``app/*.py`` incluye las
    subcarpetas de ``app``.
    """
    salida: list[str] = []
    for crudo in patrones:
        patron = crudo.strip()
        anclado = patron.startswith(("/", _ANCLA))
        patron = patron.removeprefix(_ANCLA).strip("/")
        if not patron:
            continue
        salida += _formas(patron, anclado or "/" in patron)
        if patron.startswith("**/") and patron[3:]:
            resto = patron[3:]
            salida += _formas(resto, "/" in resto)
    return list(dict.fromkeys(salida))


def es_plantilla_env(ruta: str) -> bool:
    """``.env.example`` y sus variantes (``env.example``, ``config.env.sample``…)."""
    nombre = ruta.rsplit("/", 1)[-1]
    if nombre in PLANTILLAS_ENV:
        return True
    for sufijo in SUFIJOS_PLANTILLA:
        if nombre.endswith(sufijo):
            base = nombre.removesuffix(sufijo).lower()
            return (
                base == "env"
                or base.startswith((".env", "env."))
                or base.endswith((".env", "_env", "-env"))
            )
    return False


def es_texto(ruta: str) -> bool:
    nombre = ruta.rsplit("/", 1)[-1]
    if nombre.startswith(".env") or nombre.endswith(".env"):
        return es_plantilla_env(nombre)
    if nombre in NOMBRES_TEXTO:
        return True
    _, punto, extension = nombre.rpartition(".")
    return bool(punto) and f".{extension}" in EXTENSIONES_TEXTO


@dataclass(frozen=True)
class Archivo:
    ruta: str
    objeto: str
    tamano: int


@dataclass
class Repo:
    """Un clon local, leído en ``origin/<rama>``."""

    slug: str
    ruta: Path
    rama: str | None = None

    def _git(self, *args: str, entrada: bytes | None = None) -> bytes:
        try:
            resultado = subprocess.run(
                ["git", "--no-pager", *_CONFIG_SEGURA, "-C", str(self.ruta), *args],
                input=entrada,
                capture_output=True,
                env=_entorno(),
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ErrorGit(f"{self.slug}: git {args[0]} falló: {exc}") from exc
        if resultado.returncode != 0:
            detalle = resultado.stderr.decode("utf-8", "replace").strip().splitlines()
            raise ErrorGit(f"{self.slug}: git {args[0]}: {detalle[-1] if detalle else 'error'}")
        return resultado.stdout

    @property
    def disponible(self) -> bool:
        return (self.ruta / ".git").exists()

    def referencia(self) -> str:
        """``origin/<rama>``: la declarada, la de ``origin/HEAD`` o la primera que exista."""
        if self.rama:
            candidatos = [f"origin/{self.rama}"]
        else:
            candidatos = []
            try:
                cabeza = self._git("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
                candidatos.append(cabeza.decode().strip())
            except ErrorGit:
                pass
            candidatos += ["origin/main", "origin/master", "origin/development"]
        for candidato in candidatos:
            try:
                self._git("rev-parse", "--verify", "-q", f"{candidato}^{{commit}}")
                return candidato
            except ErrorGit:
                continue
        raise ErrorGit(f"{self.slug}: no existe ninguna de {', '.join(candidatos)}")

    def commit(self, referencia: str) -> tuple[str, str]:
        """(sha corto, fecha ISO) del commit de ``referencia``."""
        salida = self._git(
            "show", "-s", "--no-show-signature", "--format=%h %cI", f"{referencia}^{{commit}}"
        )
        sha, _, fecha = salida.decode().strip().partition(" ")
        return sha, fecha

    def archivos(self, referencia: str) -> list[Archivo]:
        salida = self._git("ls-tree", "-r", "-z", "--long", referencia)
        archivos = []
        for registro in salida.split(b"\0"):
            if not registro:
                continue
            cabecera, _, ruta = registro.partition(b"\t")
            modo, tipo, objeto, tamano = cabecera.split()
            if tipo != b"blob" or modo == b"120000" or tamano == b"-":
                continue
            archivos.append(Archivo(ruta.decode("utf-8", "replace"), objeto.decode(), int(tamano)))
        return archivos

    def leer(self, archivos: list[Archivo]) -> Iterator[tuple[Archivo, str]]:
        """Contenido de texto de cada archivo (los binarios se saltan)."""
        for archivo, datos in self.leer_bytes(archivos):
            if b"\0" in datos[:8000]:
                continue
            yield archivo, datos.decode("utf-8", "replace")

    def leer_bytes(self, archivos: list[Archivo]) -> Iterator[tuple[Archivo, bytes]]:
        """Contenido crudo de cada archivo, en un solo ``git cat-file --batch``."""
        if not archivos:
            return
        entrada = "".join(f"{a.objeto}\n" for a in archivos).encode()
        salida = self._git("cat-file", "--batch", entrada=entrada)
        posicion = 0
        for archivo in archivos:
            fin_cabecera = salida.index(b"\n", posicion)
            cabecera = salida[posicion:fin_cabecera].split()
            if len(cabecera) < 3 or cabecera[1] == b"missing":
                posicion = fin_cabecera + 1
                continue
            tamano = int(cabecera[2])
            datos = salida[fin_cabecera + 1 : fin_cabecera + 1 + tamano]
            posicion = fin_cabecera + 1 + tamano + 1
            yield archivo, datos

    def leer_ruta_bytes(self, referencia: str, ruta: str) -> bytes | None:
        try:
            return self._git("cat-file", "blob", f"{referencia}:{ruta}")
        except ErrorGit:
            return None

    def leer_ruta(self, referencia: str, ruta: str) -> str | None:
        datos = self.leer_ruta_bytes(referencia, ruta)
        return None if datos is None else datos.decode("utf-8", "replace")

    def actualizar(self) -> None:
        """``git fetch`` de la rama: lo único que escribe (refs remotas).

        Con la rama declarada se usa un refspec explícito, así el fetch sólo
        puede escribir ``refs/remotes/origin/<rama>``.
        """
        if self.rama:
            refspec = f"+refs/heads/{self.rama}:refs/remotes/origin/{self.rama}"
            self._git("fetch", "--quiet", "--", "origin", refspec)
        else:
            self._git("fetch", "--quiet", "--", "origin")


def candidatos_evidencia(
    archivos: list[Archivo],
    excluir: list[str] | tuple[str, ...] = (),
    incluir_pruebas: bool = False,
    incluir_docs: bool = False,
) -> list[Archivo]:
    excluidos = list(EXCLUIR_SIEMPRE) + normalizar_globs(excluir)
    if not incluir_pruebas:
        excluidos += PATRONES_PRUEBAS
    if not incluir_docs:
        excluidos += PATRONES_DOCS
    return [
        a
        for a in archivos
        if a.tamano <= TAMANO_MAXIMO and es_texto(a.ruta) and not coincide(a.ruta, excluidos)
    ]
