import pytest

from py_backstage.catalogo import Problema, parsear_entidades
from py_backstage.modelo import Ref, enlace_seguro, parsear_ref, sin_control


def test_parsear_ref_con_y_sin_kind() -> None:
    assert parsear_ref("Pedidos-SQL", "api") == Ref("api", "default", "pedidos-sql")
    assert parsear_ref("resource:otro/db") == Ref("resource", "otro", "db")
    assert parsear_ref("api:x").corta == "api:x"
    assert parsear_ref("api:otro/x").corta == "api:otro/x"


def test_parsear_ref_namespace_por_defecto() -> None:
    # Sin namespace explícito vale el de la entidad que escribe la referencia.
    assert parsear_ref("db", "resource", "ventas") == Ref("resource", "ventas", "db")
    assert parsear_ref("otro/db", "resource", "ventas") == Ref("resource", "otro", "db")


@pytest.mark.parametrize(
    "texto", ["x", "api:", "api:a b", "api:Mal_NS/x", "api:-x", "api:" + "a" * 64]
)
def test_parsear_ref_invalida(texto: str) -> None:
    with pytest.raises(ValueError):
        parsear_ref(texto)


def test_entidad_valida() -> None:
    texto = """
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: tienda-web
  description: Tienda
  tags: [python, web]
  annotations:
    github.com/project-slug: ejemplo/tienda
spec:
  type: service
  lifecycle: production
  owner: plataforma
"""
    entidades, problemas = parsear_entidades(texto, "x.yaml")
    assert problemas == []
    assert entidades[0].ref == Ref("component", "default", "tienda-web")
    assert entidades[0].anotacion("github.com/project-slug") == "ejemplo/tienda"


@pytest.mark.parametrize(
    ("metadata", "esperado"),
    [
        ("name: Nombre con espacios", "name"),
        ("name: ok\n  namespace: Ventas", "namespace"),
        ("name: " + "a" * 64, "name"),
        ("name: ok\n  tags: [Mayuscula]", "tags"),
        ("name: ok\n  annotations: {'mal clave': x}", "annotations"),
    ],
)
def test_metadatos_invalidos(metadata: str, esperado: str) -> None:
    texto = f"""
apiVersion: backstage.io/v1alpha1
kind: Group
metadata:
  {metadata}
spec:
  type: team
  children: []
"""
    entidades, problemas = parsear_entidades(texto, "x.yaml")
    assert entidades == []
    assert any(esperado in p.mensaje for p in problemas)


def test_kind_y_spec_obligatorios() -> None:
    sin_spec = """
apiVersion: backstage.io/v1alpha1
kind: Component
metadata: {name: x}
spec: {type: service}
"""
    _, problemas = parsear_entidades(sin_spec, "x.yaml")
    assert any("spec.lifecycle" in p.mensaje for p in problemas)
    _, problemas = parsear_entidades("apiVersion: v1\nkind: Pod\nmetadata: {name: x}\n", "x.yaml")
    assert problemas


def test_yaml_invalido_y_documento_no_mapa() -> None:
    _, problemas = parsear_entidades("a: [", "x.yaml")
    assert "YAML inválido" in problemas[0].mensaje
    _, problemas = parsear_entidades("- 1\n- 2\n", "x.yaml")
    assert "mapa" in problemas[0].mensaje


def test_grupo_y_usuario_exigen_sus_listas() -> None:
    texto = """
apiVersion: backstage.io/v1alpha1
kind: Group
metadata: {name: equipo}
spec: {type: team}
---
apiVersion: backstage.io/v1alpha1
kind: User
metadata: {name: persona}
spec: {}
"""
    _, problemas = parsear_entidades(texto, "x.yaml")
    mensajes = " ".join(p.mensaje for p in problemas)
    assert "spec.children" in mensajes and "spec.memberOf" in mensajes


@pytest.mark.parametrize(
    ("url", "esperado"),
    [
        ("https://ejemplo.test/panel", "https://ejemplo.test/panel"),
        ("  http://ejemplo.test  ", "http://ejemplo.test"),
        ("https://ejemplo.test/a b(c)", "https://ejemplo.test/a%20b%28c%29"),
        ("https://ejemplo.test/x?a=1&b=<2>", "https://ejemplo.test/x?a=1&b=%3C2%3E"),
        ('https://ejemplo.test/"onmouseover=x', "https://ejemplo.test/%22onmouseover=x"),
        ("mailto:equipo@ejemplo.test", "mailto:equipo@ejemplo.test"),
        ("javascript:alert(1)", None),
        ("JavaScript:alert(1)", None),
        ("https:javascript:alert(1)", None),
        ("data:text/html,<script>", None),
        ("vbscript:x", None),
        ("ftp://ejemplo.test", None),
        ("//ejemplo.test/x", None),
        ("https://[::1", None),
    ],
)
def test_enlace_seguro(url: str, esperado: str | None) -> None:
    assert enlace_seguro(url) == esperado


def test_sin_control() -> None:
    assert sin_control("a\x1b[31m\tb\r\n\x9b") == "a\\x1b[31m\tb\\x0d\\x0a\\x9b"
    assert sin_control("ñandú 🚀") == "ñandú 🚀"
    problema = Problema("t.yaml", "nombre \x1b]0;título\x07", "api:x")
    assert str(problema) == "t.yaml [api:x]: nombre \\x1b]0;título\\x07"
