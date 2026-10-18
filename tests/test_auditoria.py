import json

from conftest import BASE, CrearCatalogo, CrearRepo, componente, git
from py_backstage.auditoria import Resultado, auditar, revisar_metadatos
from py_backstage.catalogo import cargar

USO_SQL = 'cur.execute("SELECT total FROM pedidos_pedido WHERE id = %s", [i])\n'


def _auditar(crear_catalogo: CrearCatalogo, repos: str, extra: str = "", **kw: object) -> Resultado:
    base = crear_catalogo(repos, {"todo.yaml": BASE + extra})
    return auditar(cargar(base), **kw)  # type: ignore[arg-type]


def _reglas(resultado: Resultado, regla: str) -> list[str]:
    return [h.mensaje for h in resultado.hallazgos if h.regla == regla]


REPOS = "repos:\n  - slug: ejemplo/tienda\n  - slug: ejemplo/facturador\n"


def test_v15_uso_sql_no_declarado(crear_catalogo: CrearCatalogo, crear_repo: CrearRepo) -> None:
    crear_repo("tienda", {"app/pedidos.py": USO_SQL})
    crear_repo("facturador", {"src/emitir.py": "\n\n" + USO_SQL, "tests/test_x.py": USO_SQL})
    resultado = _auditar(crear_catalogo, REPOS, componente("facturador", "ejemplo/facturador"))
    v15 = [h for h in resultado.hallazgos if h.regla == "V15"]
    # tienda-web provee la API: su propio uso no es acople.
    assert len(v15) == 1
    h = v15[0]
    assert h.repo == "ejemplo/facturador"
    assert h.objetivo == "api:pedidos-sql"
    assert "component:facturador" in h.mensaje and "consumesApis" in h.mensaje
    # Los tests no cuentan por defecto.
    assert [(e.archivo, e.linea) for e in h.evidencias] == [("src/emitir.py", 3)]
    assert {r.slug for r in resultado.auditados} == {"ejemplo/tienda", "ejemplo/facturador"}


def test_v15_declarado_no_informa(crear_catalogo: CrearCatalogo, crear_repo: CrearRepo) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo("facturador", {"src/emitir.py": USO_SQL})
    extra = componente("facturador", "ejemplo/facturador", consumesApis="[pedidos-sql]")
    resultado = _auditar(crear_catalogo, REPOS, extra)
    assert _reglas(resultado, "V15") == []
    assert _reglas(resultado, "V17") == []


def test_v15_pruebas_y_docs_a_pedido(crear_catalogo: CrearCatalogo, crear_repo: CrearRepo) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo("facturador", {"tests/test_x.py": USO_SQL, "docs/sql.md": USO_SQL})
    extra = componente("facturador", "ejemplo/facturador")
    assert _reglas(_auditar(crear_catalogo, REPOS, extra), "V15") == []
    base = crear_catalogo(REPOS, {"todo.yaml": BASE + extra}, nombre="otro")
    resultado = auditar(cargar(base), incluir_pruebas=True, incluir_docs=True)
    assert len(_reglas(resultado, "V15")) == 1


def test_v15_lee_origin_no_el_arbol(crear_catalogo: CrearCatalogo, crear_repo: CrearRepo) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    repo = crear_repo("facturador", {"src/a.py": "x = 1\n"})
    (repo / "src/sucio.py").write_text(USO_SQL, encoding="utf-8")  # sin commitear
    (repo / "src/local.py").write_text(USO_SQL, encoding="utf-8")
    git(repo, "add", "src/local.py")
    git(repo, "commit", "-q", "--no-gpg-sign", "-m", "local sin empujar")
    resultado = _auditar(crear_catalogo, REPOS, componente("facturador", "ejemplo/facturador"))
    assert _reglas(resultado, "V15") == []


def test_v15_atribucion_por_ruta(crear_catalogo: CrearCatalogo, crear_repo: CrearRepo) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo(
        "facturador",
        {
            "servicios/emisor/a.py": USO_SQL,
            "servicios/consulta/b.py": "y = 2\n",
            "comun/db.py": 'URL = os.environ["FACTURADOR_URL"]\n',
        },
    )
    extra = componente("emisor", "ejemplo/facturador", ruta="servicios/emisor") + componente(
        "consulta", "ejemplo/facturador", ruta="servicios/consulta", consumesApis="[facturas-http]"
    )
    resultado = _auditar(crear_catalogo, REPOS, extra)
    v15 = {h.objetivo: h for h in resultado.hallazgos if h.regla == "V15"}
    # El uso de SQL está en la carpeta de emisor: sólo emisor queda implicado.
    assert v15["api:pedidos-sql"].entidad == "component:emisor"
    # comun/ es de ambos; basta que uno (consulta) declare facturas-http.
    assert "api:facturas-http" not in v15


def test_v15_repo_sin_componente(crear_catalogo: CrearCatalogo, crear_repo: CrearRepo) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo("facturador", {"a.py": USO_SQL})
    resultado = _auditar(crear_catalogo, REPOS)
    v15 = [h for h in resultado.hallazgos if h.regla == "V15"]
    assert len(v15) == 1 and "el repo (sin componente)" in v15[0].mensaje
    assert any("repo sin componente" in m for m in _reglas(resultado, "V16"))


def test_v15_provisto_por_y_excluir(crear_catalogo: CrearCatalogo, crear_repo: CrearRepo) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo("facturador", {"vendor_sql/a.py": USO_SQL, "b.py": USO_SQL})
    config = (
        "repos:\n  - slug: ejemplo/tienda\n  - slug: ejemplo/facturador\n"
        "    excluir: ['vendor_sql/*']\n"
    )
    resultado = _auditar(crear_catalogo, config, componente("facturador", "ejemplo/facturador"))
    (h,) = [h for h in resultado.hallazgos if h.regla == "V15"]
    assert [e.archivo for e in h.evidencias] == ["b.py"]


def test_v17_y_sin_huellas(crear_catalogo: CrearCatalogo, crear_repo: CrearRepo) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo("facturador", {"a.py": "x = 1\n"})
    extra = componente(
        "facturador",
        "ejemplo/facturador",
        consumesApis="[pedidos-sql]",
        dependsOn="[resource:sin-huellas]",
    )
    resultado = _auditar(crear_catalogo, REPOS, extra)
    (v17,) = [h for h in resultado.hallazgos if h.regla == "V17"]
    assert v17.entidad == "component:facturador" and v17.objetivo == "api:pedidos-sql"
    assert resultado.sin_huellas == [("component:facturador", "resource:sin-huellas")]


def test_sin_clon_y_filtro(crear_catalogo: CrearCatalogo, crear_repo: CrearRepo) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    resultado = _auditar(crear_catalogo, REPOS, componente("facturador", "ejemplo/facturador"))
    assert "sin clon local" in resultado.no_auditados["ejemplo/facturador"]
    base = crear_catalogo(REPOS, {"todo.yaml": BASE}, nombre="filtro")
    resultado = auditar(cargar(base), repos=["ejemplo/tienda", "ejemplo/otro"], reglas=["V15"])
    assert [r.slug for r in resultado.auditados] == ["ejemplo/tienda"]
    assert resultado.no_auditados == {"ejemplo/otro": "no está en py-backstage.yaml"}
    assert all(h.regla == "V15" for h in resultado.hallazgos)


def test_v16_metadatos(crear_catalogo: CrearCatalogo) -> None:
    incompleto = """\
---
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: incompleto
spec: {type: service, lifecycle: production, owner: system:tienda}
"""
    config = (
        "repos:\n  - slug: ejemplo/tienda\n  - slug: ejemplo/viejo\n    ignorar: true\n"
        "  - slug: ejemplo/huerfano\n"
    )
    base = crear_catalogo(config, {"todo.yaml": BASE + incompleto})
    mensajes = [h.mensaje for h in revisar_metadatos(cargar(base, leer_repos=False))]
    assert "falta metadata.description" in mensajes
    assert "falta spec.system" in mensajes
    assert any("github.com/project-slug" in m for m in mensajes)
    assert any("debe ser un group o user" in m for m in mensajes)
    assert "ignorar: true sin motivo" in mensajes
    # Sin leer los repos, el componente de ejemplo/huerfano puede estar en su
    # catalog-info.yaml: no se acusa (test_v15_repo_sin_componente lo cubre leyéndolos).
    assert not any("repo sin componente" in m for m in mensajes)


def test_v16_componente_fuera_de_config(crear_catalogo: CrearCatalogo) -> None:
    base = crear_catalogo("repos: []\n", {"todo.yaml": BASE})
    mensajes = [h.mensaje for h in revisar_metadatos(cargar(base, leer_repos=False))]
    assert any("ejemplo/tienda no está en py-backstage.yaml" in m for m in mensajes)


def test_no_lee_env_reales_y_enmascara(
    crear_catalogo: CrearCatalogo, crear_repo: CrearRepo
) -> None:
    secreto = "s3cr3t-0123456789"
    url = f"http://admin:{secreto}@facturador.interno"
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo(
        "facturador",
        {
            ".env": f"FACTURADOR_URL={url}\n",
            "prod.env": f"FACTURADOR_URL={url}\n",
            ".env.example": "FACTURADOR_URL=\n",
            "src/config.py": f'FACTURADOR_URL = "{url}"\n',
        },
    )
    resultado = _auditar(crear_catalogo, REPOS, componente("facturador", "ejemplo/facturador"))
    v15 = [h for h in resultado.hallazgos if h.regla == "V15" and h.repo == "ejemplo/facturador"]
    assert [h.objetivo for h in v15] == ["api:facturas-http"]
    assert {e.archivo for e in v15[0].evidencias} == {".env.example", "src/config.py"}
    assert secreto not in json.dumps(resultado.como_dict())
    assert 'FACTURADOR_URL = "http://«oculto»@facturador.interno"' in {
        e.extracto for e in v15[0].evidencias
    }


def test_v16_ruta_sin_archivos(crear_catalogo: CrearCatalogo, crear_repo: CrearRepo) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo("facturador", {"servicios/emisor/a.py": "x = 1\n"})
    extra = componente("emisor", "ejemplo/facturador", ruta="servicios/emisor/") + componente(
        "consulta", "ejemplo/facturador", ruta="servicios/consulta"
    )
    resultado = _auditar(crear_catalogo, REPOS, extra)
    (h,) = [h for h in resultado.hallazgos if h.regla == "V16" and "ruta" in h.mensaje]
    assert h.entidad == "component:consulta" and h.repo == "ejemplo/facturador"
    assert "'servicios/consulta' no tiene archivos en origin/main" in h.mensaje


def test_ruta_anidada_gana_la_mas_especifica(
    crear_catalogo: CrearCatalogo, crear_repo: CrearRepo
) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo(
        "facturador",
        {"apps/api/main.py": "x = 1\n", "apps/api/admin/vista.py": USO_SQL},
    )
    extra = componente(
        "api", "ejemplo/facturador", ruta="apps/api", consumesApis="[pedidos-sql]"
    ) + componente("admin", "ejemplo/facturador", ruta="./apps/api/admin")
    resultado = _auditar(crear_catalogo, REPOS, extra)
    (v15,) = [h for h in resultado.hallazgos if h.regla == "V15"]
    # El archivo es de admin (la ruta más larga), no de api aunque api declare el uso.
    assert v15.entidad == "component:admin"
    # Y api declara pedidos-sql sin usarlo en lo que le toca.
    (v17,) = [h for h in resultado.hallazgos if h.regla == "V17"]
    assert v17.entidad == "component:api" and v17.objetivo == "api:pedidos-sql"


def test_v17_no_cuenta_plantillas_env(crear_catalogo: CrearCatalogo, crear_repo: CrearRepo) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo(
        "facturador",
        {".env.example": "FACTURADOR_URL=\n", "catalog-info.yaml": "# FACTURADOR_URL\n"},
    )
    extra = componente("facturador", "ejemplo/facturador", consumesApis="[facturas-http]")
    resultado = _auditar(crear_catalogo, REPOS, extra)
    # Declarar la variable no es usarla: V17 igual.
    (v17,) = [h for h in resultado.hallazgos if h.regla == "V17"]
    assert v17.objetivo == "api:facturas-http"
    assert _reglas(resultado, "V15") == []


def test_respuestas_de_copier_no_son_evidencia(
    crear_catalogo: CrearCatalogo, crear_repo: CrearRepo
) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo(
        "facturador",
        {".copier-answers.yml": "variables:\n- linea: FACTURADOR_URL=http://x\n"},
    )
    resultado = _auditar(crear_catalogo, REPOS, componente("facturador", "ejemplo/facturador"))
    assert _reglas(resultado, "V15") == []


def test_v16_enlace_inseguro(crear_catalogo: CrearCatalogo) -> None:
    enlaces = """\
---
apiVersion: backstage.io/v1alpha1
kind: Resource
metadata:
  name: con-enlaces
  description: Recurso con enlaces
  links:
    - url: javascript:alert(1)
      title: Malo
    - url: https://ejemplo.test/panel
    - url: mailto:equipo@ejemplo.test
spec: {type: queue, owner: plataforma, system: tienda}
"""
    base = crear_catalogo("repos: []\n", {"todo.yaml": BASE + enlaces})
    hallazgos = revisar_metadatos(cargar(base, leer_repos=False))
    malos = [h for h in hallazgos if "metadata.links" in h.mensaje]
    assert [h.entidad for h in malos] == ["resource:con-enlaces"]
    assert "'javascript:alert(1)'" in malos[0].mensaje


def test_v16_sola_revisa_las_rutas(crear_catalogo: CrearCatalogo, crear_repo: CrearRepo) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo("facturador", {"a.py": USO_SQL})
    extra = componente("consulta", "ejemplo/facturador", ruta="servicios/consulta")
    resultado = _auditar(crear_catalogo, REPOS, extra, reglas=["V16"])
    assert any("no tiene archivos en origin/main" in m for m in _reglas(resultado, "V16"))
    assert _reglas(resultado, "V15") == [] and _reglas(resultado, "V17") == []


HUELLA_LENTA = """\
---
apiVersion: backstage.io/v1alpha1
kind: Resource
metadata:
  name: lenta
  description: Huella con repeticiones anidadas
  annotations:
    py-backstage/huellas: '(\\w+\\.?)+x'
spec:
  type: external
  owner: plataforma
  system: tienda
"""


def test_v16_huella_invalida_en_metadatos(crear_catalogo: CrearCatalogo) -> None:
    base = crear_catalogo("repos: []\n", {"todo.yaml": BASE + HUELLA_LENTA})
    hallazgos = revisar_metadatos(cargar(base, leer_repos=False))
    (h,) = [h for h in hallazgos if h.entidad == "resource:lenta"]
    assert h.regla == "V16" and "huella" in h.mensaje
