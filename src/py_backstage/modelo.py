"""Entidades del catálogo con el formato de descriptores de Backstage.

Se acepta el mismo YAML que leería Backstage (``catalog-info.yaml``,
``apiVersion: backstage.io/v1alpha1``), así que el catálogo puede migrarse a un
Backstage real sin reescribirlo. Las reglas propias de py-backstage viajan en
anotaciones con el prefijo ``py-backstage/`` (Backstage las ignora).
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

API_VERSIONS = ("backstage.io/v1alpha1", "backstage.io/v1beta1")

# Mismas formas que valida Backstage (KubernetesValidatorFunctions).
_NOMBRE = re.compile(r"[a-zA-Z0-9](?:[-_.a-zA-Z0-9]{0,61}[a-zA-Z0-9])?")
# El namespace es una etiqueta DNS (RFC 1123): minúsculas, números y guiones.
_NAMESPACE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_ETIQUETA = re.compile(r"[a-z0-9:+#]+(?:-[a-z0-9:+#]+)*")
_PREFIJO_ANOTACION = re.compile(
    r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*"
)

PREFIJO = "py-backstage/"
# Anotaciones propias (todas opcionales).
ANOT_HUELLAS_SQL = PREFIJO + "huellas-sql"  # tablas o esquemas: "pedidos_, facturacion."
ANOT_HUELLAS = PREFIJO + "huellas"  # expresiones regulares, una por línea
ANOT_PROVISTO_POR = PREFIJO + "provisto-por"  # componentes exentos de declarar el uso
ANOT_RUTA = PREFIJO + "ruta"  # subcarpeta del repo que ocupa un componente
ANOT_VARIABLES = PREFIJO + "variables"  # NOMBRE=valor, una por línea (plantillas)
ANOT_EXCLUIR = PREFIJO + "excluir"  # globs del repo que no cuentan como evidencia
# Anotaciones estándar de Backstage que usamos.
ANOT_GITHUB = "github.com/project-slug"
ANOT_TECHDOCS = "backstage.io/techdocs-ref"

ESQUEMAS_ENLACE = ("http", "https", "mailto")
# Controles C0 (salvo el tabulador), DEL y C1: en una terminal, un ESC o un CSI
# leído de un repo podría reescribir la salida.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")
SLUG_GITHUB = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Enlace(_Base):
    url: str
    title: str | None = None
    icon: str | None = None


class Metadatos(_Base):
    name: str
    namespace: str = "default"
    title: str | None = None
    description: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    links: list[Enlace] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _nombre(cls, valor: str) -> str:
        if not _NOMBRE.fullmatch(valor):
            raise ValueError(
                f"{valor!r} no es un nombre válido (letras, números, '-', '_' o '.'; "
                "máximo 63; empieza y termina en letra o número)"
            )
        return valor

    @field_validator("namespace")
    @classmethod
    def _namespace(cls, valor: str) -> str:
        if not _NAMESPACE.fullmatch(valor):
            raise ValueError(f"namespace inválido {valor!r} (minúsculas, números y '-'; máximo 63)")
        return valor

    @field_validator("tags")
    @classmethod
    def _etiquetas(cls, valores: list[str]) -> list[str]:
        for valor in valores:
            if len(valor) > 63 or not _ETIQUETA.fullmatch(valor):
                raise ValueError(f"etiqueta inválida {valor!r} (minúsculas, números y '-')")
        return valores

    @field_validator("annotations", "labels")
    @classmethod
    def _claves(cls, valores: dict[str, str]) -> dict[str, str]:
        for clave in valores:
            prefijo, _, nombre = clave.rpartition("/")
            if prefijo and (len(prefijo) > 253 or not _PREFIJO_ANOTACION.fullmatch(prefijo)):
                raise ValueError(f"prefijo inválido en la clave {clave!r}")
            if not _NOMBRE.fullmatch(nombre):
                raise ValueError(f"clave inválida {clave!r}")
        return valores

    @field_validator("labels")
    @classmethod
    def _valores_etiquetas(cls, valores: dict[str, str]) -> dict[str, str]:
        for clave, valor in valores.items():
            if valor and not _NOMBRE.fullmatch(valor):
                raise ValueError(f"valor inválido {valor!r} en la etiqueta {clave!r}")
        return valores


# ── spec por tipo de entidad ────────────────────────────────────────────────


_Texto = Annotated[str, Field(min_length=1)]


class EspecComponente(_Base):
    type: _Texto
    lifecycle: _Texto
    owner: _Texto
    system: str | None = None
    subcomponentOf: str | None = None  # noqa: N815 (nombre del formato Backstage)
    providesApis: list[str] = Field(default_factory=list)  # noqa: N815
    consumesApis: list[str] = Field(default_factory=list)  # noqa: N815
    dependsOn: list[str] = Field(default_factory=list)  # noqa: N815
    dependencyOf: list[str] = Field(default_factory=list)  # noqa: N815


class EspecApi(_Base):
    type: _Texto
    lifecycle: _Texto
    owner: _Texto
    definition: _Texto
    system: str | None = None


class EspecRecurso(_Base):
    type: _Texto
    owner: _Texto
    system: str | None = None
    dependsOn: list[str] = Field(default_factory=list)  # noqa: N815
    dependencyOf: list[str] = Field(default_factory=list)  # noqa: N815


class EspecSistema(_Base):
    owner: _Texto
    domain: str | None = None


class EspecDominio(_Base):
    owner: _Texto
    subdomainOf: str | None = None  # noqa: N815


class EspecGrupo(_Base):
    type: _Texto
    children: list[str]  # obligatorio en Backstage, aunque sea []
    parent: str | None = None
    members: list[str] = Field(default_factory=list)


class EspecUsuario(_Base):
    memberOf: list[str]  # noqa: N815 (obligatorio en Backstage, aunque sea [])


class EspecUbicacion(_Base):
    type: str | None = None
    target: str | None = None
    targets: list[str] = Field(default_factory=list)


ESPECS: dict[str, type[_Base]] = {
    "Component": EspecComponente,
    "API": EspecApi,
    "Resource": EspecRecurso,
    "System": EspecSistema,
    "Domain": EspecDominio,
    "Group": EspecGrupo,
    "User": EspecUsuario,
    "Location": EspecUbicacion,
}
Tipo = Literal["Component", "API", "Resource", "System", "Domain", "Group", "User", "Location"]


@dataclass(frozen=True, order=True)
class Ref:
    """Referencia ``kind:namespace/name`` normalizada (kind en minúsculas)."""

    kind: str
    namespace: str
    name: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.namespace}/{self.name}"

    @property
    def corta(self) -> str:
        """``kind:name`` si el namespace es ``default`` (lo que se escribe a mano)."""
        if self.namespace == "default":
            return f"{self.kind}:{self.name}"
        return str(self)


def parsear_ref(
    texto: str,
    kind_por_defecto: str | None = None,
    namespace_por_defecto: str = "default",
) -> Ref:
    """Interpreta ``[kind:][namespace/]name`` como lo hace Backstage.

    Sin namespace explícito se usa el de la entidad que escribe la referencia
    (``namespace_por_defecto``), igual que en Backstage.
    """
    texto = texto.strip()
    kind, sep, resto = texto.partition(":")
    if not sep:
        kind, resto = "", texto
    namespace, sep, nombre = resto.partition("/")
    if not sep:
        namespace, nombre = namespace_por_defecto, resto
    kind = (kind or kind_por_defecto or "").lower()
    if not kind:
        raise ValueError(f"la referencia {texto!r} necesita el tipo (p. ej. 'resource:{texto}')")
    if not _NOMBRE.fullmatch(nombre) or not _NAMESPACE.fullmatch(namespace.lower()):
        raise ValueError(f"referencia inválida {texto!r}")
    return Ref(kind, namespace.lower(), nombre.lower())


class Entidad(_Base):
    apiVersion: str  # noqa: N815
    kind: str
    metadata: Metadatos
    spec: dict[str, Any] = Field(default_factory=dict)

    # Dónde se leyó (archivo y repo); no es parte del formato.
    origen: str = Field(default="", exclude=True)

    @field_validator("apiVersion")
    @classmethod
    def _version(cls, valor: str) -> str:
        if valor not in API_VERSIONS:
            raise ValueError(f"apiVersion {valor!r} no soportada (se esperaba {API_VERSIONS[0]})")
        return valor

    @field_validator("kind")
    @classmethod
    def _tipo(cls, valor: str) -> str:
        if valor not in ESPECS:
            raise ValueError(f"kind {valor!r} desconocido (válidos: {', '.join(ESPECS)})")
        return valor

    @property
    def ref(self) -> Ref:
        return Ref(self.kind.lower(), self.metadata.namespace.lower(), self.metadata.name.lower())

    @property
    def especificacion(self) -> Any:
        """El ``spec`` validado con el modelo de su ``kind``."""
        return ESPECS[self.kind].model_validate(self.spec)

    def anotacion(self, clave: str) -> str | None:
        valor = self.metadata.annotations.get(clave)
        return valor.strip() if isinstance(valor, str) and valor.strip() else None

    def lista_anotacion(self, clave: str, separador: str = ",") -> list[str]:
        valor = self.anotacion(clave)
        if not valor:
            return []
        if separador == "\n":
            partes = valor.splitlines()
        else:
            partes = valor.replace("\n", separador).split(separador)
        return [p.strip() for p in partes if p.strip()]


def enlace_seguro(url: str) -> str | None:
    """La URL lista para un enlace Markdown, o ``None`` si su esquema no es seguro.

    Sólo se aceptan ``http``, ``https`` (con host) y ``mailto``: un
    ``javascript:`` en ``metadata.links`` sería código en el portal.
    """
    url = url.strip()
    try:
        partes = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    esquema = partes.scheme.lower()
    if esquema not in ESQUEMAS_ENLACE or (esquema != "mailto" and not partes.netloc):
        return None
    # Espacios, paréntesis, comillas y '<>' romperían el enlace Markdown.
    return urllib.parse.quote(url, safe=":/?#@!$&'*+,;=%~")


def sin_control(texto: str) -> str:
    """El texto con los caracteres de control como ``\\xNN``, para imprimirlo."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", texto)


def describir_error(error: ValidationError) -> list[str]:
    """Mensajes legibles (``ruta: problema``) de un error de pydantic."""
    mensajes = []
    for detalle in error.errors():
        ruta = ".".join(str(p) for p in detalle["loc"])
        mensaje = detalle["msg"].removeprefix("Value error, ")
        mensajes.append(f"{ruta}: {mensaje}" if ruta else mensaje)
    return mensajes
