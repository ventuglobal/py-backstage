# py-backstage

Catálogo de software con el formato de [Backstage](https://backstage.io), en Python.

Sirve para lo mismo que el catálogo de Backstage: saber qué servicios, APIs y
recursos existen, quién es dueño de cada uno y quién depende de quién. A eso le
agrega una auditoría contra el código real de cada repo, que encuentra los
acoples que nadie declaró. Corre como una herramienta de línea de comandos, sin
servidor, base de datos ni Node.

| Backstage | py-backstage |
|---|---|
| `catalog-info.yaml` (`backstage.io/v1alpha1`) | El mismo YAML, validado con pydantic |
| Software Catalog | `validar`, `grafo` y un catálogo en una carpeta o en un repo git |
| TechDocs / portal | `sitio`: genera un proyecto MkDocs Material con una página por entidad y grafos Mermaid |
| Software Templates | `nuevo-servicio`: plantilla [Copier](https://copier.readthedocs.io) que crea el servicio con su `catalog-info.yaml` listo |
| Discovery | `descubrir`: repos de la organización que no están en el catálogo |
| (no existe) | `auditar`: compara lo declarado con lo que el código usa (V15, V16 y V17) |

## Instalación

Requiere Python 3.12 o superior y `git`. Sin instalar nada, con
[uv](https://docs.astral.sh/uv/):

```bash
uvx --from "git+https://github.com/ventuglobal/py-backstage@v0.1.0" py-backstage --help
```

Con los extras del portal y de la plantilla:

```bash
uvx --from "py-backstage[sitio,plantillas] @ git+https://github.com/ventuglobal/py-backstage@v0.1.0" py-backstage --help
```

El núcleo (`validar`, `auditar`, `grafo`, `validar-descriptor`) sólo depende de
pydantic y PyYAML. `sitio` necesita el extra `sitio` (MkDocs) y
`nuevo-servicio` el extra `plantillas` (Copier).

## El catálogo

Un catálogo es una carpeta con:

- `py-backstage.yaml`: la organización y los repos que se auditan;
- cualquier otro `*.yaml` o `*.yml`: entidades de Backstage (`Component`,
  `API`, `Resource`, `System`, `Domain`, `Group`, `User`, `Location`), uno o
  varios documentos por archivo.

Además se lee el `catalog-info.yaml` de la raíz de cada repo configurado, si
existe. Cada descriptor puede vivir en el catálogo central o en su propio repo,
igual que en Backstage.

Los otros tipos de Backstage (`Template` y los de plugins) no se aceptan: un
`kind` desconocido es un error de V16. Como en Backstage, una referencia sin
namespace (`api:pedidos`) toma el de la entidad que la escribe, `Group` exige
`children` y `User` exige `memberOf`, aunque sean `[]`.

```yaml
# py-backstage.yaml
org: ejemplo
raiz_clones: ~/code          # donde están los clones locales
repos:
  - slug: ejemplo/tienda
    rama: main               # se lee origin/main
  - slug: ejemplo/facturador
    ruta: facturador-api     # carpeta del clon, si no se llama como el repo
    excluir: ["scripts/**"]  # no cuenta como evidencia
  - slug: ejemplo/prototipo-2019
    ignorar: true
    motivo: archivado de hecho; no corre en producción
```

Un repo no puede aparecer dos veces en `repos` (sin distinguir mayúsculas) y
su carpeta no puede salir de `raiz_clones`.

Los globs de `excluir` (en `py-backstage.yaml` o en la anotación
`py-backstage/excluir`) siguen la semántica de `.gitignore`: sin `/`
(`*.sql`, `scripts`) calzan el nombre en cualquier nivel; con `/` al inicio o
en medio (`/seed.sql`, `app/migraciones`), la ruta desde la raíz; `dir`,
`dir/` y `dir/**` incluyen todo lo que hay dentro, y `**/x` calza `x` en
cualquier nivel. A diferencia de git, `*` también cruza `/`.

El catálogo se indica con `--catalogo` o con la variable
`PY_BACKSTAGE_CATALOGO`. Puede ser una carpeta o una carpeta dentro de un repo
git, leída desde sus objetos y no desde la copia de trabajo:

```bash
py-backstage validar --catalogo ejemplo/catalogo --sin-repos
py-backstage auditar --catalogo "git:~/code/plataforma@origin/main:catalogo"
```

La forma es `git:<repo>@<referencia>:<carpeta>`. Tanto la ruta del repo como la
referencia pueden tener `@` (`HEAD@{1}`): el repo es el prefijo más corto que
tiene `.git`.

En [`ejemplo/catalogo`](ejemplo/catalogo) hay un catálogo completo de una
organización ficticia.

### Anotaciones propias

Las reglas de py-backstage viajan en anotaciones con el prefijo
`py-backstage/`. Backstage las ignora, así que el catálogo sigue siendo
compatible.

| Anotación | En | Para qué |
|---|---|---|
| `py-backstage/huellas-sql` | API, Resource | Prefijos de tabla (`pedidos_`) o esquemas terminados en punto (`facturacion.`), separados por coma |
| `py-backstage/huellas` | API, Resource | Expresiones regulares de Python, una por línea: hosts, variables de entorno, rutas |
| `py-backstage/provisto-por` | API, Resource | Componentes que pueden usarlo sin declararlo (por ejemplo, quien lo administra) |
| `py-backstage/ruta` | Component | Subcarpeta del repo que ocupa el componente, cuando un repo tiene varios |
| `py-backstage/excluir` | Component | Globs del repo que no cuentan como evidencia |
| `py-backstage/variables` | API, Resource | `NOMBRE=valor`, una por línea; `nuevo-servicio` las copia al `.env.example` del servicio nuevo |

También se usa la anotación estándar `github.com/project-slug` para saber en qué
repo vive cada componente.

Las relaciones son las de Backstage: `dependsOn`, `consumesApis` y
`providesApis` en el componente, y `dependencyOf` escrito desde el otro lado.
Que un recurso diga `dependencyOf: [component:tienda]` equivale a que la tienda
lo declare en su `dependsOn`.

### Huellas

Una huella dice cómo se reconoce en el código que alguien usa una API o un
recurso:

```yaml
kind: API
metadata:
  name: pedidos-sql
  annotations:
    py-backstage/huellas-sql: pedidos_
---
kind: Resource
metadata:
  name: pasarela-pagos
  annotations:
    py-backstage/huellas: |
      pagos\.ejemplo\.test
      \bPAGOS_API_KEY\b
```

Las huellas SQL sólo cuentan en contexto SQL: `FROM`, `JOIN`, `INTO`,
`UPDATE`, `TABLE` (`CREATE`, `ALTER`, `DROP`), `TRUNCATE`, `COPY`, `USING`,
`REFERENCES`, `GRANT … ON`, las listas separadas por coma (`FROM a, pedidos_b`),
`db_table =`, `__tablename__ =` y `Table("…")`, con o sin comillas o tildes
invertidas. Un esquema cuenta además en `ON SCHEMA`, `CREATE SCHEMA` y
`search_path`, también como `search_path:` de YAML o como `-csearch_path=…`
(la opción de libpq en `PGOPTIONS` o en `options`). Un prefijo de tabla sólo
cuenta en el esquema por omisión: `otro.pedidos_x` no es `pedidos_`.

No son SQL los `from x import y`, `yield from` ni `raise … from` de Python,
los `import … from "x"` y `export … from "x"` de JavaScript (también de varias
líneas o `import type`), ni el `FROM imagen` y el `COPY` de un `Dockerfile`.
Un `FROM "pedidos_x"` dentro de un string SQL de JavaScript sí cuenta, aunque
la línea empiece con ``export const q = sql`…` ``.

Se rechaza, como error de V16 (también en `validar`), una huella regex que:

- calza sin texto (`^`, `a*`, `(?=x)`): marcaría cualquier archivo;
- pasa de 300 caracteres;
- anida repeticiones de largo variable (`(\w+\.?)+`, `([a-z-]+)*`): en una
  línea larga `re` puede tardar horas. Se escribe sin anidar (`[\w.]+`). El
  chequeo es conservador y también rechaza algunas formas que no explotarían.

Se leen los archivos de código y configuración más comunes (Python, Rust, Go,
JavaScript y TypeScript, Vue, Svelte, Kotlin, Java, SQL, shell, TOML, YAML,
JSON, Terraform, plantillas, `Dockerfile`, `Makefile`…). Por defecto no cuentan
los tests (`--incluir-pruebas`) ni la documentación, `README` y archivos
`.md`, `.rst`, `.txt` o `.adoc` (`--incluir-docs`). Nunca cuentan los
`catalog-info.yaml` ni los `.copier-answers.yml`: repiten las huellas del
catálogo, no son uso.

Nunca se leen, sin opción para incluirlos: `node_modules/`, `.venv/`,
`vendor/`, `dist/`, `build/` y `target/` (en cualquier nivel), los `*.lock`,
`package-lock.json`, `pnpm-lock.yaml`, `*.min.js` y `*.map`, ni los archivos
de más de 512 KB.

## Reglas

| Regla | Severidad | Qué encuentra |
|---|---|---|
| V15 | S2 | **Acople no declarado**: el código de un repo usa una API o un recurso del catálogo (según sus huellas) y ningún componente de ese repo lo declara en `dependsOn` o `consumesApis`, ni lo provee |
| V16 | S3 | **Sin descriptor o con metadatos incompletos**: repo configurado sin componente, entidad sin descripción, sistema o repo, YAML o referencias inválidas, huellas inválidas, `ignorar: true` sin motivo, enlaces que no son `http`, `https` ni `mailto`, una `py-backstage/ruta` sin archivos en la rama |
| V17 | S3 | **Dependencia declarada sin evidencia**: un componente declara usar algo que tiene huellas y su código no muestra ninguna. Una variable en `.env.example` no basta: `nuevo-servicio` la escribe ahí aunque el código todavía no la use |

En un repo con varios componentes, cada archivo se atribuye al de la
`py-backstage/ruta` más específica que lo contiene (`apps/api/admin` antes que
`apps/api`); si ninguna lo contiene, a los componentes sin ruta y, si no hay
ninguno, a todos los componentes del repo.

Cada hallazgo trae su evidencia `repo:archivo:línea`. Los repos sin clon local
aparecen como «no auditados»: nunca se omiten en silencio. `auditar --regla
V16` también lee los repos, para revisar que cada `py-backstage/ruta` tenga
archivos.

## Comandos

| Comando | Qué hace | Efectos |
|---|---|---|
| `validar` | Formato, referencias y metadatos (V16) | Sólo lectura |
| `auditar` | V15, V16 y V17 contra el código en `origin/<rama>` | Sólo lectura |
| `auditar --actualizar` | Igual, pero antes hace `git fetch` de cada repo | Escribe refs de git |
| `grafo` | Grafo Mermaid de dependencias (`--sistema` para uno solo) | Sólo lectura |
| `validar-descriptor` | Valida archivos `catalog-info.yaml` sueltos (para CI) | Sólo lectura |
| `sitio` | Genera el portal MkDocs (`--con-auditoria` agrega la última auditoría) | Escribe en `--salida` |
| `nuevo-servicio` | Crea un servicio desde la plantilla, con su `catalog-info.yaml` listo | Escribe en el destino |
| `descubrir` | Repos de la organización que no están en el catálogo (V16) | Consulta GitHub con `gh` |

`validar`, `auditar` y `descubrir` aceptan `--formato json`. Las opciones no
se abrevian (`--actual` no es `--actualizar`), así un permiso que sólo deja
correr la auditoría sin `--actualizar` no se esquiva.

Códigos de salida: `0` sin hallazgos, `1` con hallazgos, `2` error de uso o de
catálogo.

Lo que se imprime sale de repos y descriptores ajenos, así que los caracteres
de control se muestran escapados (un ESC como `\x1b`): un repo no puede
reescribir la terminal ni la salida del CI.

### Sólo lectura de verdad

`auditar` lee cada repo desde los objetos de git en `origin/<rama>`
(`git ls-tree` y `git cat-file`), nunca desde la copia de trabajo. Así no
depende de la rama en que esté el clon ni de cambios sin commitear, y no
modifica nada. Sólo `--actualizar` hace `git fetch`.

### Secretos en la evidencia

Los extractos de evidencia se enmascaran antes de mostrarse o guardarse:

- asignaciones a nombres sensibles (`*_KEY`, `*_TOKEN`, `password`, `secret`,
  `dsn`…) con `=`, `:`, `export` o `ENV` de Dockerfile. Un valor entre
  comillas, tildes invertidas o con comillas escapadas se tapa entero; al
  inicio de la línea (`.env`, YAML), sin comillas, hasta el final de la línea
  aunque tenga espacios o comas;
- valores por omisión y de respaldo de nombres sensibles
  (`os.getenv("API_KEY", "…")`, `env("SECRET", default="…")`,
  `${DB_PASSWORD:-…}`, `process.env.API_KEY || "…"`, `?? "…"`, `or "…"`);
  los de nombres que no parecen secretos (`os.getenv("PORT", "8000")`) quedan
  a la vista;
- credenciales en URLs (`usuario:clave@`, aunque la clave tenga `@`), en
  tuplas `auth=(…, "…")`, en `curl -u`, en `--password` o `--token`, en el
  `-p` de `mysql`, `docker login` y `sshpass` y en el `-a` de `redis-cli`;
- `Bearer` y `Basic`, JWT, formatos de token conocidos y cadenas largas con
  aspecto de token.

Las referencias como `os.environ["X"]`, `settings.X`, `${X}`, `{x}` o
`<clave>` no se tocan. Ante la duda se oculta de más: un nombre de 32
caracteres o más con letras y dígitos (`settings.NOMBRE_MUY_LARGO_DE_2024_V2_ABCDEFG`)
puede quedar como `«oculto»`.

De los archivos `.env` sólo se leen las plantillas: `.env.example`,
`.env.sample`, `.env.template`, `.env.dist` y las variantes con esos sufijos
(`env.example`, `.env.local.sample`, `config/prod.env.example`). Un `.env` o
`prod.env` que haya quedado en un repo nunca se abre.

El enmascarado es una red de seguridad, no un detector de secretos. Para eso
conviene usar una herramienta dedicada como gitleaks o trufflehog.

## Portal

```bash
py-backstage sitio --catalogo ejemplo/catalogo --salida sitio --con-auditoria
mkdocs serve -f sitio/mkdocs.yml
```

`sitio` genera una página por sistema y por entidad
(`entidades/<kind>/<namespace>/<nombre>`, con dueño, repo, ciclo de vida y
dependencias en ambos sentidos), el grafo Mermaid general y uno por sistema.
`--salida` puede no existir; si existe, tiene que estar vacía o haberla generado
py-backstage antes (la marca con `.py-backstage-sitio`): nunca reescribe otra.

Todo lo que viene de los descriptores se escapa: títulos, descripciones y
evidencia salen como texto, nunca como HTML o Markdown, y los enlaces sólo se
generan para `http`, `https` y `mailto`. Un descriptor de un repo ajeno no
puede inyectar código en el portal.

## Servicios nuevos ya acoplados

```bash
py-backstage nuevo-servicio ../cotizador \
  --catalogo ejemplo/catalogo \
  --nombre cotizador --descripcion "Calcula cotizaciones" \
  --sistema tienda --dueno group:plataforma \
  --depende-de api:facturas-http --postgres
```

Antes de crear nada valida contra el catálogo el sistema, el dueño y cada
dependencia, incluidos los `catalog-info.yaml` de los repos. Sin `--catalogo`
(ni `PY_BACKSTAGE_CATALOGO`) sólo revisa la forma y avisa que no comprobó que
existan. También rechaza un nombre cuyo paquete chocaría con un módulo de
Python o con una dependencia de la plantilla (`json`, `fastapi`, `class`):
conviene uno como `servicio-json`.

Después genera un servicio Python (FastAPI y uvicorn) con:

- `catalog-info.yaml` con `consumesApis` y `dependsOn` ya escritos;
- `.env.example` con las variables que declara cada dependencia
  (`py-backstage/variables`). Van comentadas, para revisarlas, las que chocan
  con una del servicio (`PORT`, `DATABASE_URL`) y las que son referencias de la
  plataforma (`${{db.DATABASE_URL}}`), que en local necesitan el valor;
- tests y CI: lint, pytest (falla si algún test se salta), Postgres opcional y
  validación del descriptor;
- `mkdocs.yml` y `docs/` para su documentación;
- `.copier-answers.yml`, para que `copier update` traiga después las mejoras de
  la plantilla.

Por defecto usa la plantilla pública de este repo en el tag de la versión
instalada (`v0.1.0` para py-backstage 0.1.0), así la CLI y la plantilla
siempre calzan. `--plantilla` acepta otra URL git o carpeta (Copier toma su
último tag, o la carpeta tal cual si no es un repo git) y `--plantilla local`
usa el checkout desde donde se corre, en `HEAD` y con los cambios sin
commitear. En ese caso avisa: el proyecto queda con un commit de la plantilla
que no existe y `copier update` no funcionará en él hasta recrearlo con los
cambios commiteados. `--ref` fija otra versión.

El servicio se genera en una carpeta temporal junto al destino y sólo se mueve
ahí si el `catalog-info.yaml` generado es válido: un error no deja un proyecto a
medias. El destino puede no existir o ser una carpeta vacía.

El servicio entra al catálogo cuando su repo se agrega a `py-backstage.yaml`
(el comando imprime la línea `- slug: org/nombre`); desde ahí lo leen
`validar` y `auditar`.

### Validar el descriptor en el CI de cualquier repo

```yaml
jobs:
  catalogo:
    uses: ventuglobal/py-backstage/.github/workflows/validar-descriptor.yml@v0.1.0
    # with:
    #   archivos: |
    #     catalog-info.yaml
    #     otro/catalog-info.yaml
```

`archivos` acepta rutas separadas por espacios o saltos de línea; no se
expanden comodines.

## Migrar a Backstage

Las entidades son descriptores de Backstage válidos. Para pasar a un Backstage
real:

1. registrar la carpeta del catálogo (o cada `catalog-info.yaml`) como
   `Location` en `app-config.yaml`;
2. las anotaciones `py-backstage/*` quedan como metadatos inertes; la auditoría
   puede seguir corriendo en CI contra el mismo catálogo;
3. los servicios creados con la plantilla ya traen `backstage.io/techdocs-ref`
   y `mkdocs.yml` para TechDocs.

## Desarrollo

```bash
uv sync --extra dev
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv run pytest
```

Los tests crean repos git sintéticos en carpetas temporales. No usan red ni la
configuración global de git.

## Licencia y marcas

Apache-2.0. Ver [LICENSE](LICENSE).

py-backstage es un proyecto independiente. No está afiliado ni respaldado por
el proyecto Backstage, Spotify ni la CNCF. El nombre Backstage se usa sólo para
indicar compatibilidad con su formato de descriptores.
