import textwrap
import time

import pytest

from py_backstage.catalogo import parsear_entidades
from py_backstage.huellas import Huellas, buscar, compilar, enmascarar
from py_backstage.modelo import Ref

API = Ref("api", "default", "pedidos")
ESQ = Ref("api", "default", "analitica")
REG = Ref("resource", "default", "pagos")

CATALOGO = """
apiVersion: backstage.io/v1alpha1
kind: API
metadata:
  name: pedidos
  annotations: {py-backstage/huellas-sql: "pedidos_, base_"}
spec: {type: sql, lifecycle: production, owner: x, definition: x}
---
apiVersion: backstage.io/v1alpha1
kind: API
metadata:
  name: analitica
  annotations: {py-backstage/huellas-sql: analitica.}
spec: {type: sql, lifecycle: production, owner: x, definition: x}
---
apiVersion: backstage.io/v1alpha1
kind: Resource
metadata:
  name: pagos
  annotations:
    py-backstage/huellas: |
      pagos\\.ejemplo\\.test
      \\bPAGOS_API_KEY\\b
spec: {type: external, owner: x}
"""


def _huellas() -> Huellas:
    entidades, problemas = parsear_entidades(CATALOGO, "c.yaml")
    assert problemas == []
    return compilar(entidades)


def _lineas(texto: str, ref: Ref) -> list[int]:
    return [c.linea for c in buscar(_huellas(), "a.py", textwrap.dedent(texto)).get(ref, [])]


def test_sql_en_varias_formas() -> None:
    texto = """\
    cur.execute("SELECT * FROM pedidos_pedido WHERE id = %s")
    q = '''
        SELECT p.id
          FROM public.pedidos_item i
          JOIN "pedidos_pedido" p ON p.id = i.pedido_id
    '''
    cur.execute("INSERT INTO pedidos_log (x) VALUES (1)")
    cur.execute("update pedidos_pedido set total = 0")
    db_table = "pedidos_pedido"
    t = Table("pedidos_item", meta)
    cur.execute("ALTER TABLE ONLY pedidos_x ADD COLUMN y int")
    cur.execute("GRANT SELECT ON pedidos_pedido TO lector")
    """
    assert _lineas(texto, API) == [1, 4, 5, 7, 8, 9, 10, 11, 12]


def test_falsos_positivos_comunes() -> None:
    texto = """\
    from base_utils import x
    from base_utils.sub import y
    import { pedidos_x } from "./pedidos_x"
    export { base_a } from "./base_a"
    base_url = "https://ejemplo.test"
    pedidos_total = sum(pedidos_lista)
    url = settings.BASE_URL
    """
    assert _lineas(texto, API) == []


def test_esquema_sql() -> None:
    texto = """\
    cur.execute("SELECT * FROM analitica.ventas")
    cur.execute("SELECT * FROM ventas")
    cur.execute("SELECT * FROM otro.pedidos_x")
    """
    assert _lineas(texto, ESQ) == [1]
    # Un prefijo de tabla sólo cuenta en el esquema por omisión (public).
    assert _lineas(texto, API) == []


def test_huellas_regex() -> None:
    texto = """\
    URL = "https://pagos.ejemplo.test/v1"
    clave = os.environ["PAGOS_API_KEY"]
    otra = os.environ["PAGOS_API_KEY_VIEJA"]
    """
    assert _lineas(texto, REG) == [1, 2]


def test_una_coincidencia_por_linea() -> None:
    texto = 'cur.execute("SELECT * FROM pedidos_a JOIN pedidos_b ON true")\n'
    assert _lineas(texto, API) == [1]


def test_huellas_invalidas() -> None:
    texto = """
apiVersion: backstage.io/v1alpha1
kind: API
metadata:
  name: mala
  annotations:
    py-backstage/huellas-sql: "pedidos-x"
    py-backstage/huellas: "(sin cerrar"
spec: {type: sql, lifecycle: production, owner: x, definition: x}
"""
    entidades, _ = parsear_entidades(texto, "c.yaml")
    huellas = compilar(entidades)
    assert len(huellas.errores) == 2
    assert huellas.objetivos == set()


@pytest.mark.parametrize(
    ("linea", "esperado"),
    [
        ("PAGOS_API_KEY=sk_live_abc123def456ghi789", "PAGOS_API_KEY=«oculto»"),
        ("PASSWORD=hunter2", "PASSWORD=«oculto»"),
        ('"password": "hunter2-muy-larga"', '"password": "«oculto»"'),
        ('client_secret: "abcDEF123456"', 'client_secret: "«oculto»"'),
        (
            "DATABASE_URL=postgresql://admin:s3cr3t@db.interno:5432/app",
            "DATABASE_URL=postgresql://«oculto»@db.interno:5432/app",
        ),
        ('h = "ghp_0123456789abcdefghijABCDEFGHIJ0123"', 'h = "«oculto»"'),
        ('x = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop0123"', 'x = "«oculto»"'),
        ("STRIPE=sk_live_51Habcdef12", "STRIPE=«oculto»"),
    ],
)
def test_enmascara_secretos(linea: str, esperado: str) -> None:
    assert enmascarar(linea) == esperado


@pytest.mark.parametrize(
    "linea",
    [
        'PAGOS_API_KEY = os.environ["PAGOS_API_KEY"]',
        "api_key = settings.PAGOS_API_KEY",
        'token = env("PAGOS_TOKEN")',
        "DATABASE_URL: ${{db.DATABASE_URL}}",
        'PAGOS_API_KEY: "${PAGOS_API_KEY}"',
        'headers = {"Authorization": f"Bearer {token}"}',
        "PAGOS_URL=https://pagos.ejemplo.test",
        "SELECT * FROM pedidos_pedido WHERE token_id = %s",
        'secret_key: str = ""',
        "auth = None",
        "session_cookie_age = 3600",
        "PAGOS_API_KEY=<clave>",
        "def normalizar_nombres_de_columnas_de_entrada_y_de_salida(x):",
    ],
)
def test_no_enmascara_referencias(linea: str) -> None:
    assert enmascarar(linea) == linea


def test_la_evidencia_sale_enmascarada() -> None:
    texto = 'PAGOS_URL = "https://pagos.ejemplo.test"\nPAGOS_API_KEY = "sk_live_0123456789abcdef"\n'
    extractos = [c.extracto for c in buscar(_huellas(), "config.py", texto)[REG]]
    assert extractos == ['PAGOS_URL = "https://pagos.ejemplo.test"', 'PAGOS_API_KEY = "«oculto»"']


def test_sql_otros_contextos() -> None:
    texto = """\
    cur.execute("TRUNCATE TABLE ONLY pedidos_a, otra, pedidos_b")
    COPY pedidos_x FROM STDIN
    DELETE FROM x USING otra o, pedidos_y p WHERE x.id = p.id
    SELECT * FROM otra a, pedidos_z b
    SELECT * FROM `pedidos_w`
    q = "SELECT * FROM \\"pedidos_v\\""
    GRANT SELECT, UPDATE ON TABLE pedidos_t TO lector
    pedido_id int REFERENCES pedidos_pedido(id)
    CREATE TABLE IF NOT EXISTS pedidos_nueva (id int)
    """
    assert _lineas(texto, API) == list(range(1, 10))


def test_esquema_en_ddl_y_search_path() -> None:
    texto = """\
    GRANT USAGE ON SCHEMA analitica TO lector
    CREATE SCHEMA IF NOT EXISTS analitica
    SET search_path TO otro, analitica
    options = "-c search_path=analitica"
    DROP SCHEMA otro
    """
    assert _lineas(texto, ESQ) == [1, 2, 3, 4]


def test_from_de_python_y_js_no_es_sql() -> None:
    py = """\
    def f():
        yield from pedidos_iter()
        raise ValueError("x") from pedidos_error
    """
    assert _lineas(py, API) == []
    js = "import {\n  a,\n} from \"pedidos_x\";\nexport * from 'pedidos_y'\n"
    assert buscar(_huellas(), "a.ts", js).get(API) is None
    # En un string SQL de JS, un identificador entre comillas sí es una tabla.
    sql_js = 'db.query(`SELECT * FROM "pedidos_v" WHERE id = $1`)\n'
    assert [c.linea for c in buscar(_huellas(), "a.ts", sql_js)[API]] == [1]


@pytest.mark.parametrize("huella", ["^", "a*", r"\b", "(?:)", "x" * 301])
def test_huella_vacia_o_larga_se_rechaza(huella: str) -> None:
    texto = f"""
apiVersion: backstage.io/v1alpha1
kind: Resource
metadata:
  name: amplia
  annotations:
    py-backstage/huellas: '{huella}'
spec: {{type: external, owner: x}}
"""
    entidades, problemas = parsear_entidades(texto, "c.yaml")
    assert problemas == []
    huellas = compilar(entidades)
    assert len(huellas.errores) == 1 and huellas.regex == []


@pytest.mark.parametrize(
    ("linea", "esperado"),
    [
        ("ENV DB_PASSWORD hunter2-largo", "ENV DB_PASSWORD «oculto»"),
        ('c = os.getenv("API_KEY", "valor-secreto")', 'c = os.getenv("API_KEY", "«oculto»")'),
        ('env("SECRET", default="valor-secreto")', 'env("SECRET", default="«oculto»")'),
        ("url: ${DB_PASSWORD:-hunter2}", "url: ${DB_PASSWORD:-«oculto»}"),
        ('get(u, auth=("usuario", "clave123"))', 'get(u, auth=("usuario", "«oculto»"))'),
        ("curl -u usuario:clave123 https://x.test", "curl -u usuario:«oculto» https://x.test"),
        ("psql --password hunter2", "psql --password «oculto»"),
        ("app --token=abc123def", "app --token=«oculto»"),
        ("mysqldump -uroot -phunter2 base", "mysqldump -uroot -p«oculto» base"),
        ("Authorization: Bearer abcdef123456", "Authorization: Bearer «oculto»"),
        ('password: str = "hunter2"', 'password: str = "«oculto»"'),
        ("export API_TOKEN=abc123", "export API_TOKEN=«oculto»"),
        ("  - SECRET_KEY: valor-secreto", "  - SECRET_KEY: «oculto»"),
        ("SECRET_KEY=valor # comentario", "SECRET_KEY=«oculto» # comentario"),
    ],
)
def test_enmascara_otras_formas(linea: str, esperado: str) -> None:
    assert enmascarar(linea) == esperado


@pytest.mark.parametrize(
    "linea",
    [
        "basic configuration of the app",
        "token: Optional[str] = None",
        "DB_URL=postgres://u:${DB_PASS}@h/db",
        "session_timeout: 30",
    ],
)
def test_no_enmascara_otras_referencias(linea: str) -> None:
    assert enmascarar(linea) == linea


@pytest.mark.parametrize(
    ("linea", "fuga"),
    [
        ('os.environ["PAGOS_API_KEY"] = "dev-clave-123"', "dev-clave"),
        ("app.config['SECRET_KEY'] = 'dev-clave'", "dev-clave"),
        ('process.env["PAGOS_API_KEY"] = "dev-clave-123"', "dev-clave"),
        ('const k = process.env.PAGOS_API_KEY || "dev-clave-123";', "dev-clave"),
        ('const k = process.env.PAGOS_API_KEY ?? "dev-clave-123";', "dev-clave"),
        ('PAGOS_API_KEY = os.getenv("PAGOS_API_KEY") or "dev-clave-123"', "dev-clave"),
        ("PAGOS_API_KEY: correct horse battery staple", "horse"),
        ("PAGOS_API_KEY=mi clave secreta", "secreta"),
        ("SECRET_KEY=abc,def", "def"),
        ('PAGOS_DB = "postgresql://admin:P@ssw0rd@pagos.ejemplo.test:5432/app"', "ssw0rd"),
        ("docker login pagos.ejemplo.test -u ci -p S3cr3t-Pass", "S3cr3t"),
        ("sshpass -p S3cr3t-Pass ssh deploy@pagos.ejemplo.test", "S3cr3t"),
        ("redis-cli -h pagos.ejemplo.test -a S3cr3t-Pass", "S3cr3t"),
        ('PAGOS_API_KEY = "mi \\"clave\\" secreta"', "secreta"),
        ("const PAGOS_API_KEY = `mi clave secreta`;", "secreta"),
    ],
)
def test_no_deja_secretos_a_la_vista(linea: str, fuga: str) -> None:
    assert fuga not in enmascarar(linea)


@pytest.mark.parametrize(
    "linea",
    [
        "Authorization: `Bearer ${t}`",
        'x = d["token"]',
    ],
)
def test_no_enmascara_plantillas_ni_lecturas(linea: str) -> None:
    assert enmascarar(linea) == linea


def test_espacios_finales_quedan_fuera_del_valor() -> None:
    assert enmascarar("SECRET_KEY=valor  ") == "SECRET_KEY=«oculto»  "
    assert enmascarar("SECRET_KEY=valor , # nota") == "SECRET_KEY=«oculto» , # nota"


@pytest.mark.parametrize(
    "linea",
    [
        "x " + "a" * 200_000,
        "0123456789abcdef" * 12_000,
        "key" * 60_000,
        '"\\' * 100_000,
        "x://u:" + "b" * 200_000,
        "key or " * 30_000,
        "docker login " * 15_000,
        "redis-cli " * 20_000,
        "mysql " * 30_000,
        "key: a" + " " * 200_000 + "x",
        "key=a" + " " * 200_000 + "x",
        "eyJ" * 60_000,
    ],
    ids=lambda linea: repr(linea[:12]),
)
def test_enmascarar_es_lineal(linea: str) -> None:
    inicio = time.perf_counter()
    enmascarar(linea)
    assert time.perf_counter() - inicio < 2


@pytest.mark.parametrize(
    ("ruta", "texto"),
    [
        ("a.ts", "import " + 'from "pedidos_x" ' * 12_000),
        ("a.py", "yield " + "from pedidos_x " * 12_000),
        ("a.ts", '}\nfrom "pedidos_x"\n' * 10_000),
        ("a.js", "a" * 200_000 + ' from "pedidos_x"' * 2_000),
        ("a.sql", "grant " * 30_000 + " on pedidos_x"),
        ("a.sql", 'from "pedidos_' * 12_000),
    ],
    ids=["import-js", "yield", "lineas", "minificado", "grant", "ident-abierto"],
)
def test_buscar_es_lineal(ruta: str, texto: str) -> None:
    inicio = time.perf_counter()
    buscar(_huellas(), ruta, texto)
    assert time.perf_counter() - inicio < 2


@pytest.mark.parametrize(
    ("ruta", "texto", "esperado"),
    [
        ("a.ts", 'export const listar = () => db.$queryRaw`SELECT * FROM "pedidos_x"`\n', [1]),
        ("a.ts", 'export default sql`SELECT * FROM "pedidos_x"`\n', [1]),
        ("a.ts", 'import x from "y"; const q = `SELECT * FROM "pedidos_x"`\n', [1]),
        ("a.sh", "export Q='SELECT * FROM \"pedidos_x\"'\n", [1]),
        ("a.ts", 'import type { A } from "pedidos_x"\n', []),
        ("a.ts", "export * as ns from 'pedidos_x'\n", []),
        ("a.ts", 'import x, { y } from "pedidos_x"\n', []),
        ("a.ts", 'import { a,\n  b } from "pedidos_x";\n', []),
        ("a.ts", 'import {\n  a,\n}\nfrom "pedidos_x";\n', []),
        ("Dockerfile", "COPY pedidos_api/ /app/pedidos_api/\n", []),
        ("Dockerfile", "FROM python:3.12 AS base_build\nFROM base_build\nFROM base_runtime\n", []),
        ("app.Dockerfile", "FROM base_build\n", []),
        ("Dockerfile", 'RUN psql -c "SELECT * FROM pedidos_x"\n', [1]),
        ("a.sql", "DROP TABLE otra, pedidos_x;\n", [1]),
        ("a.sql", "LOCK TABLE a, pedidos_x;\n", [1]),
        ("a.sql", "ALTER TABLE IF EXISTS ONLY pedidos_x ADD c int;\n", [1]),
    ],
)
def test_sql_imports_y_dockerfile(ruta: str, texto: str, esperado: list[int]) -> None:
    assert [c.linea for c in buscar(_huellas(), ruta, texto).get(API, [])] == esperado


@pytest.mark.parametrize(
    ("ruta", "texto"),
    [
        ("a.py", 'connect_args={"options": "-csearch_path=analitica"}\n'),
        ("a.sh", 'PGOPTIONS="-csearch_path=analitica"\n'),
        ("a.yaml", "      search_path: analitica\n"),
        ("a.sql", "SET search_path TO 'otro', 'analitica';\n"),
    ],
)
def test_search_path_en_otras_formas(ruta: str, texto: str) -> None:
    assert [c.linea for c in buscar(_huellas(), ruta, texto).get(ESQ, [])] == [1]


@pytest.mark.parametrize(
    "huella",
    [r"(\w+\.?)+ejemplo\.test", r"([a-z0-9-]+)*\.ejemplo", r"(?=pagos\.ejemplo\.test)"],
)
def test_huella_peligrosa_se_rechaza(huella: str) -> None:
    texto = f"""
apiVersion: backstage.io/v1alpha1
kind: Resource
metadata:
  name: r
  annotations:
    py-backstage/huellas: '{huella}'
spec: {{type: external, owner: x}}
"""
    entidades, _ = parsear_entidades(texto, "c.yaml")
    huellas = compilar(entidades)
    assert len(huellas.errores) == 1 and huellas.regex == []


@pytest.mark.parametrize(
    "huella", [r"pagos\.ejemplo\.test", r"(https?://)?api\.ejemplo\.test/v\d+", r"(?:\d{1,3}\.){3}"]
)
def test_huella_normal_se_acepta(huella: str) -> None:
    texto = f"""
apiVersion: backstage.io/v1alpha1
kind: Resource
metadata:
  name: r
  annotations:
    py-backstage/huellas: '{huella}'
spec: {{type: external, owner: x}}
"""
    entidades, _ = parsear_entidades(texto, "c.yaml")
    assert compilar(entidades).errores == []


def test_varias_huellas_en_una_linea_se_enmascaran_una_vez() -> None:
    texto = 'x = "https://pagos.ejemplo.test"; PAGOS_API_KEY = "sk_live_0123456789abcdef"\n'
    salida = buscar(_huellas(), "a.py", texto)[REG]
    assert len(salida) == 1 and "sk_live" not in salida[0].extracto
