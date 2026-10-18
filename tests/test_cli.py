import json
from pathlib import Path

import pytest

from conftest import BASE, CrearCatalogo, CrearRepo, componente
from py_backstage import descubrir
from py_backstage.cli import main

REPOS = "repos:\n  - slug: ejemplo/tienda\n  - slug: ejemplo/facturador\n"


def test_validar(crear_catalogo: CrearCatalogo, capsys: pytest.CaptureFixture[str]) -> None:
    sano = crear_catalogo("repos:\n  - slug: ejemplo/tienda\n", {"t.yaml": BASE}, nombre="sano")
    assert main(["validar", "--catalogo", str(sano), "--sin-repos"]) == 0
    assert "V16: 0" in capsys.readouterr().out
    roto = crear_catalogo("repos: []\n", {"t.yaml": BASE}, nombre="roto")
    assert main(["validar", "--catalogo", str(roto), "--formato", "json"]) == 1
    datos = json.loads(capsys.readouterr().out)
    mensajes = [h["mensaje"] for h in datos["hallazgos"]]
    assert any("ejemplo/tienda no está en py-backstage.yaml" in m for m in mensajes)


def test_auditar_json(
    crear_catalogo: CrearCatalogo, crear_repo: CrearRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    crear_repo("tienda", {"a.py": "x = 1\n"})
    crear_repo("facturador", {"a.py": 'cur.execute("SELECT 1 FROM pedidos_x")\n'})
    base = crear_catalogo(REPOS, {"t.yaml": BASE + componente("facturador", "ejemplo/facturador")})
    capsys.readouterr()
    assert main(["auditar", "--catalogo", str(base), "--formato", "json"]) == 1
    datos = json.loads(capsys.readouterr().out)
    assert [h["regla"] for h in datos["hallazgos"]] == ["V15"]
    assert datos["hallazgos"][0]["evidencias"][0]["archivo"] == "a.py"
    assert main(["auditar", "--catalogo", str(base), "--regla", "V17"]) == 0
    assert "V17: 0" in capsys.readouterr().out


def test_errores_de_uso(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validar"]) == 2
    assert main(["auditar", "--catalogo", str(tmp_path / "no")]) == 2
    with pytest.raises(SystemExit):
        main(["auditar", "--regla", "V99"])


def test_grafo_y_descriptor(
    crear_catalogo: CrearCatalogo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = crear_catalogo("repos: []\n", {"t.yaml": BASE})
    assert main(["grafo", "--catalogo", str(base), "--sin-repos"]) == 0
    assert capsys.readouterr().out.startswith("flowchart LR")
    assert main(["grafo", "--catalogo", str(base), "--sin-repos", "--sistema", "otro"]) == 2

    bueno = tmp_path / "bueno.yaml"
    bueno.write_text(componente("x", "ejemplo/x"), encoding="utf-8")
    malo = tmp_path / "malo.yaml"
    malo.write_text(
        "apiVersion: backstage.io/v1alpha1\nkind: Component\nmetadata: {name: y}\n"
        "spec: {type: service, lifecycle: production, owner: z}\n",
        encoding="utf-8",
    )
    assert main(["validar-descriptor", str(bueno)]) == 0
    assert main(["validar-descriptor", str(bueno), str(malo)]) == 1
    salida = capsys.readouterr().out
    assert "falta metadata.description" in salida and "falta spec.system" in salida
    assert main(["validar-descriptor", str(tmp_path / "no.yaml")]) == 2


def test_descubrir(
    crear_catalogo: CrearCatalogo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = crear_catalogo("org: ejemplo\n" + REPOS, {"t.yaml": BASE})
    remotos = [
        descubrir.RepoRemoto("ejemplo/tienda", False, False, "private"),
        descubrir.RepoRemoto("ejemplo/nuevo", False, False, "private"),
        descubrir.RepoRemoto("ejemplo/viejo", True, False, "private"),
    ]
    pedidos: list[str] = []

    def listar(org: str, limite: int = 500) -> list[descubrir.RepoRemoto]:
        pedidos.append(org)
        return remotos

    monkeypatch.setattr(descubrir, "listar", listar)
    assert main(["descubrir", "--catalogo", str(base)]) == 1
    salida = capsys.readouterr().out
    assert pedidos == ["ejemplo"]
    assert "ejemplo/nuevo" in salida and "ejemplo/viejo" not in salida
    assert main(["descubrir", "--catalogo", str(base), "--incluir-archivados"]) == 1
    assert "ejemplo/viejo" in capsys.readouterr().out


def test_sitio_cli(crear_catalogo: CrearCatalogo, tmp_path: Path) -> None:
    base = crear_catalogo("repos: []\n", {"t.yaml": BASE})
    assert main(["sitio", "--catalogo", str(base), "--salida", str(tmp_path / "s")]) == 0
    assert (tmp_path / "s" / "docs" / "index.md").exists()
    ajena = tmp_path / "ajena"
    ajena.mkdir()
    (ajena / "x").write_text("x", encoding="utf-8")
    assert main(["sitio", "--catalogo", str(base), "--salida", str(ajena)]) == 2


@pytest.mark.parametrize(
    "argumentos",
    [
        # Un permiso que bloquea --actualizar no se esquiva con una abreviatura.
        ["auditar", "--actual"],
        ["auditar", "--actualiza"],
        ["audi"],
        ["--vers"],
    ],
)
def test_sin_abreviaturas(argumentos: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as salida:
        main(argumentos)
    assert salida.value.code == 2


def test_validar_descriptor_tras_doble_guion(tmp_path: Path) -> None:
    raro = tmp_path / "-raro.yaml"
    raro.write_text(componente("x", "ejemplo/x"), encoding="utf-8")
    assert main(["validar-descriptor", "--", str(raro)]) == 0


DESCRIPTOR = """\
apiVersion: backstage.io/v1alpha1
kind: Component
metadata: {name: cotizador}
spec: {type: service, lifecycle: experimental, owner: plataforma}
"""


def test_nuevo_servicio_sin_catalogo_avisa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def run_copy(fuente: str, destino: Path, **opciones: object) -> None:
        destino.mkdir(parents=True, exist_ok=True)
        (destino / "catalog-info.yaml").write_text(DESCRIPTOR, encoding="utf-8")

    monkeypatch.setattr("copier.run_copy", run_copy)
    argumentos = ["nuevo-servicio", str(tmp_path / "cotizador"), "--nombre", "cotizador"]
    argumentos += ["--descripcion", "Cotiza", "--sistema", "tienda", "--dueno", "plataforma"]
    assert main([*argumentos, "--org", "ejemplo"]) == 0
    salida = capsys.readouterr()
    assert "sin catálogo, no se validó" in salida.err
    assert "- slug: ejemplo/cotizador" in salida.out
