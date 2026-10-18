import ast
import keyword
import re
import shutil
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from conftest import BASE, CrearCatalogo, CrearRepo, git
from py_backstage import __version__
from py_backstage.catalogo import cargar, parsear_entidades
from py_backstage.nuevo import (
    DEPENDENCIAS_PLANTILLA,
    PLANTILLA_PUBLICA,
    ErrorNuevo,
    Pedido,
    aviso_plantilla_sucia,
    crear,
    plantilla_local,
    preparar,
)

RAIZ = Path(__file__).resolve().parents[1]


def _pedido(**cambios: object) -> Pedido:
    datos: dict[str, object] = {
        "nombre": "cotizador",
        "descripcion": "Cotiza envíos",
        "sistema": "tienda",
        "dueno": "plataforma",
        "org": "ejemplo",
        "dependencias": ["api:pedidos-sql", "api:facturas-http", "resource:sin-huellas"],
    }
    datos.update(cambios)
    return Pedido(**datos)  # type: ignore[arg-type]


@pytest.fixture
def plantilla(tmp_path: Path) -> Path:
    """Copia de la plantilla fuera de git: Copier la usa tal cual, sin commits."""
    destino = tmp_path / "plantilla"
    destino.mkdir()
    shutil.copy(RAIZ / "copier.yml", destino / "copier.yml")
    shutil.copytree(RAIZ / "plantillas", destino / "plantillas")
    return destino


def test_plantilla_local() -> None:
    assert plantilla_local() == RAIZ


def test_preparar(crear_catalogo: CrearCatalogo) -> None:
    catalogo = cargar(crear_catalogo("repos: []\n", {"t.yaml": BASE}), leer_repos=False)
    datos = preparar(catalogo, _pedido())
    assert datos["consume_apis"] == ["api:pedidos-sql", "api:facturas-http"]
    assert datos["depende_de"] == ["resource:sin-huellas"]
    assert datos["dueno"] == "group:plataforma"
    assert datos["variables"] == [
        {"linea": "FACTURADOR_URL=http://facturador.interno:8000", "fuente": "api:facturas-http"}
    ]


@pytest.mark.parametrize(
    ("cambios", "mensaje"),
    [
        ({"nombre": "Con Espacios"}, "minúsculas"),
        ({"nombre": "tienda-web"}, "ya existe"),
        ({"sistema": "otro"}, "system:otro no existe"),
        ({"dueno": "nadie"}, "group:nadie no existe"),
        ({"dependencias": ["api:no-existe"]}, "api:no-existe no existe"),
        ({"dependencias": ["system:tienda"]}, "sólo se depende"),
    ],
)
def test_preparar_rechaza(
    crear_catalogo: CrearCatalogo, cambios: dict[str, object], mensaje: str
) -> None:
    catalogo = cargar(crear_catalogo("repos: []\n", {"t.yaml": BASE}), leer_repos=False)
    with pytest.raises(ErrorNuevo, match=mensaje):
        preparar(catalogo, _pedido(**cambios))


def test_variable_mal_escrita(crear_catalogo: CrearCatalogo) -> None:
    mala = BASE.replace("FACTURADOR_URL=http", "facturador url http")
    catalogo = cargar(crear_catalogo("repos: []\n", {"t.yaml": mala}), leer_repos=False)
    with pytest.raises(ErrorNuevo, match="variable mal escrita"):
        preparar(catalogo, _pedido())


@pytest.mark.parametrize("usa_postgres", [False, True])
def test_crear_renderiza_proyecto_acoplado(
    crear_catalogo: CrearCatalogo, plantilla: Path, tmp_path: Path, usa_postgres: bool
) -> None:
    catalogo = cargar(crear_catalogo("repos: []\n", {"t.yaml": BASE}), leer_repos=False)
    datos = preparar(catalogo, _pedido(usa_postgres=usa_postgres))
    destino = tmp_path / "cotizador"
    crear(destino, datos, str(plantilla))

    entidades, problemas = parsear_entidades(
        (destino / "catalog-info.yaml").read_text(encoding="utf-8"), "catalog-info.yaml"
    )
    assert problemas == []
    (entidad,) = entidades
    assert entidad.spec["consumesApis"] == ["api:pedidos-sql", "api:facturas-http"]
    assert entidad.spec["dependsOn"] == ["resource:sin-huellas"]
    assert entidad.anotacion("github.com/project-slug") == "ejemplo/cotizador"

    env = (destino / ".env.example").read_text(encoding="utf-8")
    assert "# api:facturas-http\nFACTURADOR_URL=http://facturador.interno:8000" in env
    assert ("DATABASE_URL=" in env) is usa_postgres
    assert (destino / "tests/test_db.py").exists() is usa_postgres
    assert (destino / "src/cotizador/main.py").exists()
    respuestas = yaml.safe_load((destino / ".copier-answers.yml").read_text(encoding="utf-8"))
    assert respuestas["nombre"] == "cotizador"
    ci = yaml.safe_load((destino / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert ("services" in ci["jobs"]["tests"]) is usa_postgres
    assert "catalogo" in ci["jobs"]
    assert "${{ github.ref }}" in (destino / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def test_crear_no_pisa(plantilla: Path, tmp_path: Path) -> None:
    destino = tmp_path / "ocupado"
    destino.mkdir()
    (destino / "x").write_text("x", encoding="utf-8")
    with pytest.raises(ErrorNuevo, match="no está vacía"):
        crear(destino, {}, str(plantilla))


@pytest.mark.parametrize(
    ("cambios", "mensaje"),
    [
        ({"descripcion": "  "}, "descripción es obligatoria"),
        ({"descripcion": "dos\nlíneas"}, "en una línea"),
        ({"org": "-x"}, "organización de GitHub inválida"),
        ({"org": "a b"}, "organización de GitHub inválida"),
        ({"sistema": "api:pedidos-sql"}, "debe ser un system"),
        ({"dueno": "system:tienda"}, "debe ser un group o user"),
        ({"sistema": "a/b/c"}, "referencia inválida"),
        ({"dependencias": ["api:"]}, "referencia inválida"),
    ],
)
def test_preparar_rechaza_entradas(
    crear_catalogo: CrearCatalogo, cambios: dict[str, object], mensaje: str
) -> None:
    catalogo = cargar(crear_catalogo("repos: []\n", {"t.yaml": BASE}), leer_repos=False)
    with pytest.raises(ErrorNuevo, match=mensaje):
        preparar(catalogo, _pedido(**cambios))


def test_preparar_sin_catalogo_conserva_el_namespace() -> None:
    datos = preparar(None, _pedido(sistema="system:ventas/tienda", dueno="user:ana"))
    assert datos["sistema"] == "ventas/tienda"
    assert datos["dueno"] == "user:ana"
    assert datos["variables"] == []


def test_descripcion_con_comillas_y_barras(
    crear_catalogo: CrearCatalogo, plantilla: Path, tmp_path: Path
) -> None:
    catalogo = cargar(crear_catalogo("repos: []\n", {"t.yaml": BASE}), leer_repos=False)
    descripcion = 'Cotiza "envíos" desde C:\\rutas\\ y más\\'
    destino = tmp_path / "cotizador"
    crear(destino, preparar(catalogo, _pedido(descripcion=descripcion)), str(plantilla))

    (entidad,), _ = parsear_entidades(
        (destino / "catalog-info.yaml").read_text(encoding="utf-8"), "catalog-info.yaml"
    )
    assert entidad.metadata.description == descripcion
    pyproject = tomllib.loads((destino / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["description"] == descripcion
    modulo = ast.parse((destino / "src/cotizador/__init__.py").read_text(encoding="utf-8"))
    assert ast.get_docstring(modulo) == descripcion


DESCRIPTOR = """\
apiVersion: backstage.io/v1alpha1
kind: Component
metadata: {name: cotizador}
spec: {type: service, lifecycle: experimental, owner: plataforma}
"""


@pytest.fixture
def copier_falso(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Reemplaza ``copier.run_copy``: guarda los argumentos y escribe el descriptor."""
    llamada: dict[str, object] = {"descriptor": DESCRIPTOR}

    def run_copy(fuente: str, destino: Path, **opciones: object) -> None:
        llamada.update(fuente=fuente, **opciones)
        destino.mkdir(parents=True, exist_ok=True)
        if llamada["descriptor"] is not None:
            (destino / "catalog-info.yaml").write_text(str(llamada["descriptor"]), "utf-8")

    monkeypatch.setattr("copier.run_copy", run_copy)
    return llamada


@pytest.mark.parametrize(
    ("plantilla", "ref", "esperado"),
    [
        (None, None, f"v{__version__}"),
        (PLANTILLA_PUBLICA, None, f"v{__version__}"),
        (str(RAIZ), None, "HEAD"),
        ("gh:otra/plantilla", None, None),
        (None, "main", "main"),
        (str(RAIZ), "v0.0.9", "v0.0.9"),
    ],
)
def test_crear_elige_la_version_de_la_plantilla(
    copier_falso: dict[str, object],
    tmp_path: Path,
    plantilla: str | None,
    ref: str | None,
    esperado: str | None,
) -> None:
    fuente = crear(tmp_path / "nuevo", {}, plantilla, ref)
    assert fuente == (plantilla or PLANTILLA_PUBLICA)
    assert copier_falso["vcs_ref"] == esperado
    assert copier_falso["unsafe"] is False


@pytest.mark.parametrize(
    ("descriptor", "mensaje"),
    [
        (None, "no generó"),
        (DESCRIPTOR.replace("Component", "Servicio"), "catalog-info.yaml inválido"),
        (DESCRIPTOR + "---\n" + DESCRIPTOR, "2 entidades"),
    ],
)
def test_crear_valida_el_descriptor_generado(
    copier_falso: dict[str, object], tmp_path: Path, descriptor: str | None, mensaje: str
) -> None:
    copier_falso["descriptor"] = descriptor
    with pytest.raises(ErrorNuevo, match=mensaje):
        crear(tmp_path / "nuevo", {})


@pytest.mark.parametrize("nombre", ["json", "fastapi", "pytest", "class", "typing-extensions"])
def test_preparar_rechaza_paquetes_reservados(nombre: str) -> None:
    with pytest.raises(ErrorNuevo, match="chocaría"):
        preparar(None, _pedido(nombre=nombre, dependencias=[]))


def test_copier_conoce_los_nombres_reservados() -> None:
    preguntas = yaml.safe_load((RAIZ / "copier.yml").read_text(encoding="utf-8"))
    reservados = set(preguntas["nombres_reservados"]["default"])
    stdlib = {n for n in sys.stdlib_module_names if re.fullmatch(r"[a-z][a-z0-9_]*", n)}
    # El paquete va en minúsculas: False, None y True no pueden darse.
    palabras = {k for k in keyword.kwlist if k.islower()}
    assert palabras | DEPENDENCIAS_PLANTILLA | stdlib <= reservados


def test_preparar_valida_el_tipo_sin_catalogo() -> None:
    with pytest.raises(ErrorNuevo, match="sólo se depende"):
        preparar(None, _pedido(dependencias=["system:tienda"]))


def test_preparar_sin_descriptores_de_los_repos(crear_catalogo: CrearCatalogo) -> None:
    catalogo = cargar(crear_catalogo("repos: []\n", {"t.yaml": BASE}), leer_repos=False)
    with pytest.raises(ErrorNuevo, match="sin leer los catalog-info.yaml"):
        preparar(catalogo, _pedido(dependencias=["api:de-otro-repo"]))


def test_preparar_con_repos_sin_leer(crear_catalogo: CrearCatalogo) -> None:
    config = "repos:\n  - slug: ejemplo/fantasma\n"
    catalogo = cargar(crear_catalogo(config, {"t.yaml": BASE}))
    with pytest.raises(ErrorNuevo, match="no se leyeron los descriptores de ejemplo/fantasma"):
        preparar(catalogo, _pedido(dependencias=["api:de-otro-repo"]))


def test_variables_que_chocan_o_son_referencias_van_comentadas(
    crear_catalogo: CrearCatalogo, plantilla: Path, tmp_path: Path
) -> None:
    variables = (
        "      FACTURADOR_URL=http://facturador.interno:8000\n"
        "      PORT=9000\n"
        "      DATABASE_URL=${{db.DATABASE_URL}}\n"
        "      CACHE_URL=${{redis.URL}}\n"
    )
    base = BASE.replace("      FACTURADOR_URL=http://facturador.interno:8000\n", variables)
    catalogo = cargar(crear_catalogo("repos: []\n", {"t.yaml": base}), leer_repos=False)
    datos = preparar(catalogo, _pedido(usa_postgres=True))
    notas = {v["linea"].partition("=")[0]: v.get("nota") for v in datos["variables"]}
    assert notas == {
        "FACTURADOR_URL": None,
        "PORT": "choca con una variable de este servicio",
        "DATABASE_URL": "choca con una variable de este servicio",
        "CACHE_URL": "referencia de la plataforma: en local, poner el valor",
    }
    destino = tmp_path / "cotizador"
    crear(destino, datos, str(plantilla))
    env = (destino / ".env.example").read_text(encoding="utf-8")
    activas = [linea for linea in env.splitlines() if linea and not linea.startswith("#")]
    assert activas == [
        "LOG_LEVEL=INFO",
        "PORT=8000",
        "DATABASE_URL=postgresql://postgres:postgres@localhost:5432/cotizador",
        "FACTURADOR_URL=http://facturador.interno:8000",
    ]
    assert "# PORT=9000" in env and "# CACHE_URL=${{redis.URL}}" in env
    readme = (destino / "README.md").read_text(encoding="utf-8")
    assert "`CACHE_URL` (de `api:facturas-http`): comentada en `.env.example`" in readme


def test_crear_destino_que_es_archivo(copier_falso: dict[str, object], tmp_path: Path) -> None:
    archivo = tmp_path / "archivo"
    archivo.write_text("x", encoding="utf-8")
    with pytest.raises(ErrorNuevo, match="no es una carpeta"):
        crear(archivo, {})


def test_crear_no_deja_restos_si_falla(copier_falso: dict[str, object], tmp_path: Path) -> None:
    copier_falso["descriptor"] = DESCRIPTOR.replace("Component", "Servicio")
    with pytest.raises(ErrorNuevo, match="inválido"):
        crear(tmp_path / "nuevo", {})
    assert list(tmp_path.iterdir()) == []


def test_crear_en_carpeta_vacia(copier_falso: dict[str, object], tmp_path: Path) -> None:
    destino = tmp_path / "vacia"
    destino.mkdir()
    crear(destino, {})
    assert (destino / "catalog-info.yaml").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["vacia"]


def test_crear_usa_la_ruta_absoluta_de_la_plantilla(
    copier_falso: dict[str, object],
    plantilla: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(plantilla.parent)
    assert crear(tmp_path / "nuevo", {}, plantilla.name) == str(plantilla.resolve())
    assert copier_falso["fuente"] == str(plantilla.resolve())
    assert copier_falso["vcs_ref"] is None


def test_aviso_plantilla_sucia(crear_repo: CrearRepo) -> None:
    repo = crear_repo("plantilla", {"copier.yml": "a: 1\n", "plantillas/x.jinja": "x\n"})
    assert aviso_plantilla_sucia(str(repo), "HEAD") is None
    (repo / "plantillas/x.jinja").write_text("y\n", encoding="utf-8")
    aviso = aviso_plantilla_sucia(str(repo), "HEAD")
    assert aviso is not None and "cambios sin commitear" in aviso
    assert aviso_plantilla_sucia(str(repo), "v0.1.0") is None
    git(repo, "commit", "-qam", "cambio", "--no-gpg-sign")
    assert aviso_plantilla_sucia(str(repo), "HEAD") is None
    assert aviso_plantilla_sucia("gh:otra/plantilla", "HEAD") is None
