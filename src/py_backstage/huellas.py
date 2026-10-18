"""Huellas: cómo se reconoce en el código que alguien usa una API o un recurso.

Dos clases, declaradas como anotaciones en la entidad usada:

``py-backstage/huellas-sql``
    Prefijos de tabla (``pedidos_``, ``inventario_item``) o esquemas terminados en
    punto (``facturacion.``), separados por coma. Sólo cuentan en contexto
    SQL (``FROM``, ``JOIN``, ``INTO``, ``UPDATE``, ``TABLE`` con su lista,
    ``TRUNCATE``, ``COPY``, ``USING``, ``REFERENCES``, ``GRANT … ON``,
    ``db_table =``, ``__tablename__ =``, ``Table("…")``; los esquemas también
    en ``ON SCHEMA``, ``CREATE SCHEMA`` y ``search_path``, incluso
    ``-csearch_path=…``), así que ``pedidos_url``, los ``import`` de Python o
    de JavaScript y el ``FROM imagen`` de un Dockerfile no son evidencia.

``py-backstage/huellas``
    Expresiones regulares de Python, una por línea (hosts, nombres de
    variables de entorno, rutas de API): ``pagos\\.ejemplo\\.test``,
    ``\\bFACTURADOR_URL\\b``. Se rechazan las que calzan sin texto y las que
    anidan repeticiones (``(\\w+\\.?)+``), que pueden colgar la auditoría.
"""

from __future__ import annotations

import bisect
import importlib
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .modelo import ANOT_HUELLAS, ANOT_HUELLAS_SQL, Entidad, Ref

MAX_LARGO_REGEX = 300
_MAX_LISTA = 20  # nombres seguidos por coma después de FROM, USING, …

# Identificador SQL: simple, entre comillas dobles (Postgres; también escapadas
# dentro de un string) o invertidas (MySQL).
_IDENT = r'(?:\\?"[^"\\\n]+\\?"|`[^`\n]+`|[a-z_][a-z0-9_$]*)'
_SQL = re.compile(
    r"""
    \b(?:
        (?P<desde>from)\s+(?:only\s+)?
      | (?P<lista>
            using\s+
          | truncate\s+(?:table\s+)?(?:only\s+)?
          | table\s+(?:if\s+(?:not\s+)?exists\s+)?(?:only\s+)?
          | grant\b[^;]{0,200}?\bon\s+(?:table\s+)?
        )
      | (?P<copia>copy)\s+
      | (?:join|into|update|references)\s+(?:if\s+(?:not\s+)?exists\s+)?(?:only\s+)?
      | (?:db_table|__tablename__)\s*[=:]\s*["']
      | Table\(\s*["']
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_NOMBRE_SQL = re.compile(rf"(?:(?P<esquema>{_IDENT})\s*\.\s*)?(?P<tabla>{_IDENT})", re.I)
# Alias opcional y coma: sigue otro nombre de la lista.
_SIGUE = re.compile(r"(?:\s+(?:as\s+)?[a-z_][a-z0-9_]*)?\s*,\s*", re.I)
# También ``-csearch_path=…`` (opción de libpq pegada a ``-c``), ``search_path:
# …`` (YAML) y listas con cada nombre entre comillas (``TO 'otro', 'analitica'``).
_ESQUEMA = re.compile(
    rf"""
    (?:\b|(?<=-c))(?:
        (?:on|in)\s+schema\s+
      | (?:create|alter|drop)\s+schema\s+(?:if\s+(?:not\s+)?exists\s+)?
      | search_path(?:\s*[=:]\s*|\s+to\s+)
    )
    (?P<nombres>["']?{_IDENT}["']?(?:\s*,\s*["']?{_IDENT}["']?)*)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_UN_IDENT = re.compile(_IDENT, re.I)
_IMPORT_PY = re.compile(r"(?:\.\w+)*[^\S\n]+c?import\b")
# ``import … from`` / ``export … from``: entre la palabra y el ``from`` no hay
# ``=``, comillas, backticks ni paréntesis (``export const q = sql`… FROM "t"```
# sí es SQL).
_IMPORT_JS = re.compile(r"""\s*(?:import|export)\b[^=`'"();]*""")
# Línea que cierra un import de varias líneas: ``}``, ``  b }``, ``  c as d }``.
_CIERRE_JS = re.compile(r"[\w\s,$]*\}\s*")
_YIELD = re.compile(r"\byield\s*$")
_RAISE = re.compile(r"\s*raise\b")
# Lo que se mira antes de un ``from`` para saber si es un import. Una línea
# más larga (código minificado) no se reconoce como import: mirarla entera en
# cada ``from`` sería cuadrático.
_MAX_ANTES = 1000
# Dockerfile: ``FROM imagen`` y ``COPY origen destino`` no son SQL.
_DOCKERFILE = re.compile(r"(?:docker|container)file(?:\..*)?|.*\.(?:docker|container)file", re.I)
_EXT_PY = (".py", ".pyi")
_EXT_JS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte")


@dataclass(frozen=True)
class Coincidencia:
    ruta: str
    linea: int
    extracto: str


@dataclass
class Huellas:
    """Huellas compiladas de todo el catálogo."""

    # prefijo de tabla -> entidades; esquema -> entidades
    tablas: dict[str, set[Ref]] = field(default_factory=dict)
    esquemas: dict[str, set[Ref]] = field(default_factory=dict)
    regex: list[tuple[Ref, re.Pattern[str]]] = field(default_factory=list)
    errores: list[tuple[Ref, str]] = field(default_factory=list)

    @property
    def objetivos(self) -> set[Ref]:
        salida = {r for refs in self.tablas.values() for r in refs}
        salida |= {r for refs in self.esquemas.values() for r in refs}
        return salida | {r for r, _ in self.regex}


# Analizador interno de ``re`` (no está en typeshed). Si una versión futura lo
# cambia, las dos revisiones que lo usan dejan de rechazar, sin fallar.
try:
    _sre: Any = importlib.import_module("re._parser")
except ImportError:  # pragma: no cover
    _sre = None


def _arbol(patron: re.Pattern[str]) -> Any:
    try:
        return _sre.parse(patron.pattern, patron.flags)
    except Exception:  # pragma: no cover
        return None


def _vacia(patron: re.Pattern[str]) -> bool:
    """Si la regex puede calzar sin consumir nada (``^``, ``a*``, ``\\b``, ``(?=x)``…)."""
    if patron.search("") is not None:
        return True
    if any(m.start() == m.end() for m in patron.finditer("a Z_9-./\n:")):
        return True
    arbol = _arbol(patron)
    try:
        return arbol is not None and int(arbol.getwidth()[0]) == 0
    except Exception:  # pragma: no cover
        return False


def _repeticion_anidada(patron: re.Pattern[str]) -> bool:
    """Si una repetición sin tope contiene otra de largo variable (``(\\w+\\.?)+``).

    Con esas formas ``re`` prueba un número exponencial de particiones y una
    sola línea larga puede colgar la auditoría. Es conservador: también rechaza
    formas que no explotan (``(\\w+\\.)+``), que se escriben sin anidar
    (``[\\w.]+``).
    """
    arbol = _arbol(patron)
    if arbol is None:
        return False
    repeticiones = {_sre.MAX_REPEAT, _sre.MIN_REPEAT, _sre.POSSESSIVE_REPEAT}

    def recorrer(nodos: Any, dentro: bool) -> bool:
        for op, arg in nodos:
            if op in repeticiones:
                minimo, maximo, cuerpo = arg
                if dentro and maximo != minimo:
                    return True
                # Una repetición posesiva no retrocede: lo de adentro no explota.
                if op != _sre.POSSESSIVE_REPEAT and recorrer(
                    cuerpo, dentro or maximo == _sre.MAXREPEAT
                ):
                    return True
                continue
            hijos: list[Any] = []
            if op == _sre.SUBPATTERN:
                hijos = [arg[-1]]
            elif op == _sre.BRANCH:
                hijos = list(arg[1])
            elif op in (_sre.ASSERT, _sre.ASSERT_NOT):
                hijos = [arg[1]]
            elif op == _sre.GROUPREF_EXISTS:
                hijos = [h for h in arg[1:] if h is not None]
            if any(recorrer(h, dentro) for h in hijos):
                return True
        return False

    try:
        return recorrer(arbol, False)
    except Exception:  # pragma: no cover
        return False


def compilar(entidades: list[Entidad]) -> Huellas:
    huellas = Huellas()
    for entidad in entidades:
        for texto in entidad.lista_anotacion(ANOT_HUELLAS_SQL):
            clave = texto.lower().strip('"')
            if not re.fullmatch(r"[a-z_][a-z0-9_]*\.?", clave):
                huellas.errores.append((entidad.ref, f"huella SQL inválida {texto!r}"))
                continue
            destino = huellas.esquemas if clave.endswith(".") else huellas.tablas
            destino.setdefault(clave.rstrip("."), set()).add(entidad.ref)
        for texto in entidad.lista_anotacion(ANOT_HUELLAS, separador="\n"):
            if len(texto) > MAX_LARGO_REGEX:
                huellas.errores.append(
                    (entidad.ref, f"huella de más de {MAX_LARGO_REGEX} caracteres")
                )
                continue
            try:
                patron = re.compile(texto, re.MULTILINE)
            except re.error as exc:
                huellas.errores.append((entidad.ref, f"huella inválida {texto!r}: {exc}"))
                continue
            if _vacia(patron):
                huellas.errores.append(
                    (entidad.ref, f"huella {texto!r} calza sin texto: lo calzaría todo")
                )
                continue
            if _repeticion_anidada(patron):
                huellas.errores.append(
                    (
                        entidad.ref,
                        f"huella {texto!r} anida repeticiones (como '(\\w+\\.?)+'), que en una "
                        "línea larga pueden tardar horas: escribirla sin anidar ('[\\w.]+')",
                    )
                )
                continue
            huellas.regex.append((entidad.ref, patron))
    return huellas


# ── Enmascarado ─────────────────────────────────────────────────────────────
# La evidencia se imprime y va al portal, así que una línea con un secreto
# versionado no debe salir tal cual. Es una red de seguridad: ante la duda,
# oculta de más.

_ENMASCARADO = "«oculto»"
# Un identificador entero (``os.environ.PAGOS_API_KEY``) que contiene una
# palabra sensible. Empieza donde empieza el identificador y lo consume sin
# retroceder: si no, una tirada larga (un hex de 500 KB) es cuadrática.
_NOMBRE_SECRETO = (
    r"(?<![A-Za-z0-9_.-])(?=[A-Za-z0-9_.-]*?(?:key|secret|token|passw(?:or)?d|pwd|pass|clave|"
    r"credential|auth|dsn|private|signature|cookie|session))[A-Za-z0-9_.-]++"
)
# Un valor entre comillas (con escapes; también backticks de JS), con prefijo
# de Python opcional (r, b, f…); las triples pueden quedar abiertas (docstring
# de varias líneas).
_COMILLAS = (
    r'[rbfuRBFU]{0,2}(?:"""[^\n]*?(?:"""|$)|'
    r"'''[^\n]*?(?:'''|$)|"
    r'"(?:[^"\\\n]|\\.)*+"|'
    r"'(?:[^'\\\n]|\\.)*+'|"
    r"`(?:[^`\\\n]|\\.)*+`)"
)
_SEPARADOR = r"(?::=|=>|==|!=|=|:)"
# ``password: str = "x"``: la anotación de tipo antes del ``=``. Las
# repeticiones de este bloque y del siguiente son posesivas o terminan en un
# carácter que no es espacio: con una tirada larga de espacios, la forma
# ingenua (``[… ]*?\s*``) es cúbica.
_TIPO = r"(?::[^\S\n]*+[A-Za-z_][\w.\[\], |\t]*+(?==))?"
# ``X_KEY = …``, ``"password": …``, ``os.environ["X_KEY"] = …``.
_ASIGNACION_SECRETA = re.compile(
    r"(?P<nombre>[\"']?" + _NOMBRE_SECRETO + r"[\"']?\]?\s*" + _TIPO + _SEPARADOR + r"\s*)"
    r"(?P<valor>" + _COMILLAS + r"|(?:(?:bearer|basic|token|digest)\s+)?[^\s,;)}\]\"'`]+)",
    re.IGNORECASE,
)
# Al inicio de la línea (``.env``, YAML): el valor sin comillas llega hasta el
# final, menos una coma final y un `` # comentario``. No aplica a una anotación
# de tipo de Python (``token: str = …``).
_ASIGNACION_LINEA = re.compile(
    r"^(?P<nombre>(?:export\s+|-\s+)?" + _NOMBRE_SECRETO + r"(?:=|\s*:\s+))"
    r"(?![A-Za-z_][\w.\[\], |\t]*+=(?!=))"
    r"(?P<valor>[^\s\"'#](?:[^\n]*?\S)?)(?P<resto>(?:\s*+,)?(?:\s++#.*)?\s*+)$",
    re.IGNORECASE,
)
_ENV_DOCKER = re.compile(
    r"^(?P<nombre>ENV\s+" + _NOMBRE_SECRETO + r"\s+)(?P<valor>[^=\s][^\n]*)$", re.I
)
# os.getenv("API_KEY", "valor") / env("SECRET", default="valor")
_DEFECTO_LLAMADA = re.compile(
    r"(?P<nombre>[\"']" + _NOMBRE_SECRETO + r"[\"']\s*,\s*(?:default\s*=\s*)?)"
    r"(?P<valor>" + _COMILLAS + r")",
    re.IGNORECASE,
)
# process.env.X_KEY || "valor", env["X_TOKEN"] ?? "valor", os.getenv("X_KEY") or "valor"
_RESPALDO = re.compile(
    r"(?P<nombre>" + _NOMBRE_SECRETO + r"[\"'\]]*\)?\s*(?:\|\||\?\?|\bor\b)\s*)"
    r"(?P<valor>" + _COMILLAS + r")",
    re.IGNORECASE,
)
# ${DB_PASSWORD:-valor}
_DEFECTO_SHELL = re.compile(
    r"(?P<nombre>\$\{" + _NOMBRE_SECRETO + r":?[-=])(?P<valor>[^}\n]+)", re.IGNORECASE
)
# auth=("usuario", "clave"), HTTPBasicAuth("usuario", "clave")
_TUPLA_AUTH = re.compile(
    r"(?P<nombre>\b(?:\w*auth|credentials?)\s*(?:=\s*)?[(\[]\s*" + _COMILLAS + r"\s*,\s*)"
    r"(?P<valor>" + _COMILLAS + r")",
    re.IGNORECASE,
)
# curl -u usuario:clave, --user=usuario:clave
_USUARIO_CLAVE = re.compile(
    r"(?P<nombre>(?:^|\s)(?:-u|--user)(?:\s+|=)[\"']?[^\s:\"']*:)(?P<valor>[^\s\"']+)"
)
# --password valor, --token=valor
_OPCION_SECRETA = re.compile(
    r"(?P<nombre>(?:^|\s)--?(?:password|passwd|pass|pwd|token|secret|api[-_]?key)"
    r"(?:\s+|=)[\"']?)(?P<valor>[^\s\"']+)",
    re.IGNORECASE,
)
# mysql -pclave (pegado a la opción; -p sola pide la clave)
_MYSQL = re.compile(
    r"(?P<nombre>\b(?:mysql|mysqldump|mysqladmin|mariadb)\b[^\n]{0,200}?\s-p)(?P<valor>[^\s\"']+)"
)
# Opciones cortas: docker login -p clave, sshpass -p clave, redis-cli -a clave.
# La opción se busca a 200 caracteres del comando como máximo: sin tope, una
# línea que repite el comando es cuadrática.
_OPCION_CORTA = re.compile(
    r"(?P<nombre>\b(?:(?:docker|podman|buildah|skopeo|helm)\s+(?:registry\s+)?login\b"
    r"[^\n]{0,200}?\s-p(?:\s+|=)|sshpass\s+-p\s*|redis-cli\b[^\n]{0,200}?\s-a\s+)[\"']?)"
    r"(?P<valor>[^\s\"']+)"
)
# La clave llega hasta la última ``@`` antes del host (``u:P@ss@host``).
_CREDENCIAL_URL = re.compile(
    r"(?<![a-z0-9+.-])(?P<esquema>[a-z][a-z0-9+.-]*+://)(?P<usuario>[^\s/@:]*):"
    r"(?P<clave>[^\s/\"'`]+)@",
    re.I,
)
_BEARER = re.compile(
    r"\b(?P<esquema>Bearer|Basic|BEARER|BASIC)\s+(?P<valor>[A-Za-z0-9._~+/-]{8,}=*)"
)
_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]{8,}")
_TOKEN_CONOCIDO = re.compile(
    r"\b(?:(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|xox[abeprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{35}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|glpat-[A-Za-z0-9_-]{20,}|"
    r"shp(?:at|ca|pa|ss)_[A-Fa-f0-9]{32}|APP_USR-[0-9A-Za-z-]{20,})"
)
# 32+ caracteres de alfabeto de token con al menos una letra y un dígito (los
# identificadores largos sin dígitos no se tocan).
_TOKEN_LARGO = re.compile(
    r"(?<![A-Za-z0-9+/_-])(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])"
    r"[A-Za-z0-9+/_-]{32,}={0,2}(?![A-Za-z0-9+/_-])"
)
_ABRE = re.compile(r"([rbfuRBFU]{0,2})(\"\"\"|'''|\"|'|`)")
_ESQUEMA_AUTH = re.compile(r"(?:bearer|basic|token|digest)\s+", re.IGNORECASE)
_NO_SECRETOS = {
    "none", "null", "nil", "true", "false", "str", "int", "bytes", "bool", "self", "lambda",
}  # fmt: skip


def _es_literal(valor: str, *, entre_comillas: bool) -> bool:
    """True si el valor parece un secreto escrito en el código, no una referencia.

    Nunca cuentan las plantillas (``{…}``, ``${…}``) ni los marcadores (``<…>``,
    ``%s``, ``?``). Entre comillas, lo demás es literal. Sin comillas tampoco
    cuentan las llamadas, los subíndices (``os.environ[…]``), los atributos
    (``settings.X``), los números cortos ni ``None``/``True``/tipos; lo demás sí
    (``X=abc`` en un ``.env``).
    """
    limpio = valor.strip()
    if not limpio or "{" in limpio or limpio.startswith(("$", "%", "?", "<")):
        return False
    if entre_comillas:
        return True
    if limpio.lower() in _NO_SECRETOS or "(" in limpio or "[" in limpio:
        return False
    if re.fullmatch(r"-?\d{1,9}(?:\.\d+)?", limpio):
        return False
    return not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", limpio)


def _ocultar(valor: str) -> str:
    """El valor enmascarado, con sus comillas y su esquema (``Bearer …``)."""
    abre = _ABRE.match(valor)
    inicio, interior, cierra = "", valor, ""
    if abre:
        comilla = abre.group(2)
        inicio, interior = abre.group(0), valor[abre.end() :]
        if interior.endswith(comilla):
            interior, cierra = interior[: -len(comilla)], comilla
    esquema = _ESQUEMA_AUTH.match(interior)
    if esquema:
        inicio, interior = inicio + esquema.group(0), interior[esquema.end() :]
    if _ENMASCARADO in interior or not _es_literal(interior, entre_comillas=abre is not None):
        return valor
    return f"{inicio}{_ENMASCARADO}{cierra}"


def _reemplazo(m: re.Match[str]) -> str:
    return m.group("nombre") + _ocultar(m.group("valor")) + (m.groupdict().get("resto") or "")


def _url(m: re.Match[str]) -> str:
    clave = m.group("clave")
    if "{" in clave or clave.startswith(("$", "%", "<")):
        return m.group(0)
    return f"{m.group('esquema')}{_ENMASCARADO}@"


def _bearer(m: re.Match[str]) -> str:
    valor = m.group("valor")
    if re.fullmatch(r"[a-z]+", valor):  # «Basic configuration»: prosa, no credencial
        return m.group(0)
    return f"{m.group('esquema')} {_ENMASCARADO}"


def enmascarar(extracto: str) -> str:
    """Oculta valores con forma de secreto en una línea de evidencia.

    Cubre asignaciones a nombres sensibles (``X_KEY = …``, ``password: …``,
    ``d["X_KEY"] = …``, ``ENV TOKEN …``, ``"X_SECRET", "defecto"``,
    ``${X_TOKEN:-defecto}``, ``process.env.X_KEY || "defecto"``), credenciales
    en URLs y en opciones de línea de comandos (``-u u:clave``, ``--password``,
    ``mysql -p…``, ``docker login -p``, ``sshpass -p``, ``redis-cli -a``),
    ``Authorization: Bearer …``, tuplas ``auth=(…)``, JWT, formatos de token
    conocidos y cadenas largas con forma de token.
    """
    extracto = _CREDENCIAL_URL.sub(_url, extracto)
    for patron in (
        _TUPLA_AUTH,
        _DEFECTO_LLAMADA,
        _DEFECTO_SHELL,
        _RESPALDO,
        _USUARIO_CLAVE,
        _OPCION_SECRETA,
        _OPCION_CORTA,
        _MYSQL,
        _ENV_DOCKER,
        # Antes que la asignación general: el valor sin comillas de un ``.env``
        # o un YAML llega hasta el final de la línea, con espacios y comas.
        _ASIGNACION_LINEA,
        _ASIGNACION_SECRETA,
    ):
        extracto = patron.sub(_reemplazo, extracto)
    extracto = _BEARER.sub(_bearer, extracto)
    for patron in (_JWT, _TOKEN_CONOCIDO, _TOKEN_LARGO):
        extracto = patron.sub(_ENMASCARADO, extracto)
    return extracto


# ── Búsqueda ────────────────────────────────────────────────────────────────


def _saltos(texto: str) -> list[int]:
    salida = []
    posicion = texto.find("\n")
    while posicion != -1:
        salida.append(posicion)
        posicion = texto.find("\n", posicion + 1)
    return salida


def _sin_comillas(ident: str | None) -> str:
    return (ident or "").replace("\\", "").strip('"`').lower()


def _inicio_linea(texto: str, posicion: int) -> int | None:
    """Dónde empieza la línea de ``posicion``, o ``None`` si queda a más de ``_MAX_ANTES``."""
    desde = max(0, posicion - _MAX_ANTES)
    salto = texto.rfind("\n", desde, posicion)
    if salto == -1 and desde > 0:
        return None
    return salto + 1


def _es_import(texto: str, m: re.Match[str], fin_nombre: int, ruta: str) -> bool:
    """Si un ``from`` es de un import (Python o JS) o de ``yield/raise … from``."""
    if _IMPORT_PY.match(texto, fin_nombre):
        return True
    inicio_linea = _inicio_linea(texto, m.start())
    if inicio_linea is None:
        return False
    antes = texto[inicio_linea : m.start()]
    # ``import x from "y"`` o el cierre de uno de varias líneas (``} from "y"``,
    # o ``from "y"`` solo, después de una línea ``}``); ``FROM "tabla"`` en un
    # string SQL de JS no es un import.
    siguiente = texto[m.end() : m.end() + 1]
    if siguiente in ('"', "'"):
        if _IMPORT_JS.fullmatch(antes):
            return True
        if ruta.endswith(_EXT_JS):
            if _CIERRE_JS.fullmatch(antes):
                return True
            if not antes.strip() and inicio_linea > 0:
                inicio_previa = _inicio_linea(texto, inicio_linea - 1)
                if inicio_previa is not None and _CIERRE_JS.fullmatch(
                    texto, inicio_previa, inicio_linea - 1
                ):
                    return True
    if ruta.endswith(_EXT_PY):
        return bool(_YIELD.search(antes) or _RAISE.match(antes))
    return False


def _nombres(texto: str, posicion: int, lista: bool) -> list[re.Match[str]]:
    """Nombres (``esquema.tabla``) que siguen a la palabra clave SQL."""
    salida = []
    for _ in range(_MAX_LISTA if lista else 1):
        m = _NOMBRE_SQL.match(texto, posicion)
        if not m:
            break
        salida.append(m)
        sigue = _SIGUE.match(texto, m.end()) if lista else None
        if not sigue:
            break
        posicion = sigue.end()
    return salida


def _es_dockerfile(ruta: str) -> bool:
    return bool(_DOCKERFILE.fullmatch(PurePosixPath(ruta).name))


def _al_inicio_de_linea(texto: str, posicion: int) -> bool:
    inicio = _inicio_linea(texto, posicion)
    return inicio is not None and not texto[inicio:posicion].strip()


def _aciertos(huellas: Huellas, nombres: list[re.Match[str]]) -> list[tuple[Ref, int]]:
    """Las entidades que usan cada nombre (``esquema.tabla``) y dónde aparece."""
    salida = []
    for nombre in nombres:
        esquema = _sin_comillas(nombre.group("esquema"))
        tabla = _sin_comillas(nombre.group("tabla"))
        posicion = nombre.start("tabla")
        salida += [(ref, posicion) for ref in huellas.esquemas.get(esquema, ())]
        if esquema in ("", "public"):
            for prefijo, refs in huellas.tablas.items():
                if tabla.startswith(prefijo):
                    salida += [(ref, posicion) for ref in refs]
    return salida


def buscar(huellas: Huellas, ruta: str, texto: str) -> dict[Ref, list[Coincidencia]]:
    """Coincidencias de cada entidad en un archivo (una por línea y entidad)."""
    encontradas: dict[Ref, dict[int, Coincidencia]] = {}
    saltos: list[int] | None = None
    # Una línea con varias coincidencias se enmascara una sola vez: se
    # enmascara entera (cortarla antes podría partir un secreto y dejar la mitad
    # a la vista).
    extractos: dict[int, str] = {}

    def anotar(ref: Ref, posicion: int) -> None:
        nonlocal saltos
        if saltos is None:
            saltos = _saltos(texto)
        indice = bisect.bisect_left(saltos, posicion)
        lineas = encontradas.setdefault(ref, {})
        if indice + 1 in lineas:
            return
        extracto = extractos.get(indice)
        if extracto is None:
            inicio = saltos[indice - 1] + 1 if indice else 0
            fin = saltos[indice] if indice < len(saltos) else len(texto)
            extracto = enmascarar(texto[inicio:fin].strip())
            if len(extracto) > 160:
                extracto = extracto[:157] + "…"
            extractos[indice] = extracto
        lineas[indice + 1] = Coincidencia(ruta, indice + 1, extracto)

    dockerfile = _es_dockerfile(ruta)

    if huellas.tablas or huellas.esquemas:
        minusculas = texto.lower()
        if any(p in minusculas for p in huellas.tablas) or any(
            e in minusculas for e in huellas.esquemas
        ):
            for m in _SQL.finditer(texto):
                nombres = _nombres(texto, m.end(), lista=bool(m.group("desde") or m.group("lista")))
                aciertos = _aciertos(huellas, nombres)
                if not aciertos:
                    continue
                if (
                    dockerfile
                    and (m.group("desde") or m.group("copia"))
                    and _al_inicio_de_linea(texto, m.start())
                ):
                    continue
                if m.group("desde") and _es_import(texto, m, nombres[0].end(), ruta):
                    continue
                for ref, posicion in aciertos:
                    anotar(ref, posicion)
            if huellas.esquemas:
                for m in _ESQUEMA.finditer(texto):
                    for ident in _UN_IDENT.finditer(m.group("nombres")):
                        for ref in huellas.esquemas.get(_sin_comillas(ident.group(0)), ()):
                            anotar(ref, m.start("nombres") + ident.start())

    for ref, patron in huellas.regex:
        for m in patron.finditer(texto):
            if m.end() > m.start():
                anotar(ref, m.start())

    return {
        ref: sorted(lineas.values(), key=lambda c: c.linea) for ref, lineas in encontradas.items()
    }
