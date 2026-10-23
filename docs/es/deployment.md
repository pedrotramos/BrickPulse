# Despliegue

[English](../en/deployment.md) · **Español** · [Português](../pt-BR/deployment.md)

> *Traducido de la versión en inglés. Si hay diferencias, la versión en inglés es la referencia.*

BrickPulse se instala en un workspace de Databricks como un
[Declarative Automation Bundle](https://docs.databricks.com/dev-tools/bundles/) (paquete de automatización
declarativa). `databricks.yml` y `resources/` declaran tres recursos:

| Recurso | Qué es |
| --- | --- |
| App `brickpulse` | La Databricks App (FastAPI + UI de React compilada) |
| Pipeline `precompute` | Un pipeline serverless de Lakeflow que evalúa las reglas y escribe las tablas `finops_*` |
| Job `daily_refresh` | Ejecuta el pipeline y luego el tracker de ahorros, todos los días a las 05:00 UTC |

En el repositorio no se guarda ningún valor de workspace y no hay un target por defecto. Siempre se
elige el workspace de forma explícita con `--profile` y con el target (`dev` o `prod`).

## Requisitos previos

**En el equipo local**

- La configuración local de [primeros pasos](getting-started.md), incluido el `.venv` con el paquete `build`.
- La [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html), autenticada en el workspace
  de destino mediante un perfil (`databricks auth login --host <workspace-url> --profile <PROFILE>`).
- Un frontend compilado. `src/brickpulse/server/static` está versionado, así que solo hace falta
  `npm run build` después de cambiar el frontend.

**En el workspace**

- Un **catálogo** de Unity Catalog en el que se pueda crear un esquema. BrickPulse crea `brickpulse`
  (o `brickpulse_dev` para el target `dev`).
- Un **SQL warehouse** con permiso `CAN_USE`. Las verificaciones del despliegue y la app consultan a
  través de él.
- Permiso para crear Databricks Apps, pipelines y jobs.
- **Acceso de lectura a las system tables** que se indican abajo, concedido al usuario que realiza el
  despliegue. El pipeline se ejecuta con esa identidad.

### Tablas del sistema

Obligatorias. El despliegue se detiene si alguna de estas tablas no se puede leer:

```text
system.billing.usage            system.billing.list_prices
system.access.audit             system.access.workspaces_latest
system.compute.clusters         system.compute.warehouses
system.lakeflow.jobs            system.lakeflow.pipelines
```

Opcionales. Si alguna de estas no se puede leer, el despliegue desactiva solo las reglas que dependen de ella:

```text
system.access.table_lineage                 system.ai_gateway.usage
system.alert.alerts                         system.alert.alert_evaluation_history
system.billing.account_prices               system.billing.cloud_infra_cost
system.compute.instance_events              system.compute.instance_pools
system.compute.node_timeline                system.compute.node_types
system.compute.warehouse_events             system.lakeflow.job_run_timeline
system.lakeflow.job_task_run_timeline       system.lakeflow.job_tasks
system.lakeflow.pipeline_update_timeline    system.query.history
system.serving.endpoint_usage               system.serving.served_entities
system.sharing.materialization_history      system.storage.predictive_optimization_operations_history
```

`system.billing.account_prices` pasa a ser obligatoria con `--pricing-mode account`. Si no hay acceso, el
despliegue imprime los `GRANT` exactos que debe ejecutar un administrador del metastore, por ejemplo:

```sql
GRANT USE CATALOG ON CATALOG system TO `you@example.com`;
GRANT USE SCHEMA ON SCHEMA system.billing TO `you@example.com`;
GRANT SELECT ON TABLE system.billing.usage TO `you@example.com`;
```

> [!NOTE]
> Las system tables solo cubren los workspaces de la **región del metastore**. BrickPulse muestra el gasto
> de la cuenta que puede ver en esa región; no es una vista de cuenta entre regiones.

## Primera instalación

```sh
.venv/bin/python scripts/deploy.py --profile <PROFILE> --target prod \
  --catalog <CATALOG> --warehouse-id <WAREHOUSE_ID>
```

El script ejecuta estos pasos:

1. Verifica el usuario, el catálogo y el warehouse.
2. Prueba el acceso de lectura a cada system table que utiliza. Si falta una obligatoria, se detiene e
   imprime los `GRANT` necesarios. Si falta una opcional, desactiva las reglas que dependen de ella.
   Usar `--stop-on-missing` para detenerse también en ese caso.
3. Escribe los valores elegidos en `.databricks/bundle/<target>/variable-overrides.json`. Esa carpeta
   está fuera de git, y las ejecuciones posteriores reutilizan los valores.
4. Crea el esquema.
5. Ejecuta `databricks bundle deploy`.
6. Ejecuta el pipeline y el tracker una vez. La primera ejecución lee 30 días de system tables.
7. Concede al service principal de la app `SELECT`, tabla por tabla, nunca sobre el esquema completo.
   También intenta conceder lectura sobre las system tables que la app consulta en vivo. Eso requiere un
   administrador del metastore. Si falla, la app sigue funcionando y el script imprime los comandos que
   debe ejecutar un administrador.
8. Inicia la app e imprime su URL.

Al principio, solo el usuario que despliega puede abrir la app (`CAN_MANAGE`), además de los
administradores del workspace, que heredan el acceso. Ver [seguridad](security.md) para dar acceso a más
personas.

Ejecutar primero con `--dry-run` para hacer solo las verificaciones y la validación del bundle.

## Actualizar

Desplegar una nueva versión no ejecuta ningún job:

```sh
git pull
databricks bundle deploy -t prod --profile <PROFILE>
databricks bundle run brickpulse -t prod --profile <PROFILE>
```

Si la versión cambia reglas, SQL o el pipeline, los datos solo reflejan el cambio después de una
actualización. Ejecutar el job de actualización ahora, o esperar a la siguiente ejecución diaria:

```sh
databricks bundle run daily_refresh -t prod --profile <PROFILE>
```

`scripts/deploy.py --profile <PROFILE> --target prod --refresh` despliega, actualiza y vuelve a aplicar
los permisos sobre las tablas nuevas en un solo paso.

## Opciones del script de despliegue

| Flag | Efecto |
| --- | --- |
| `--profile` (obligatorio) | Perfil de la Databricks CLI del workspace de destino |
| `--target` (obligatorio) | `dev` o `prod` |
| `--catalog`, `--warehouse-id` | Obligatorios en el primer despliegue a un target; después se recuerdan |
| `--schema` | Reemplaza el esquema por defecto de cada target |
| `--pricing-mode auto\|list\|account` | Fuente de precios. Ver [configuración](configuration.md#modo-de-precios) |
| `--lookback-days N` | Días de historial que lee cada actualización (1–90, por defecto 30) |
| `--apps-tab` / `--no-apps-tab` | Mostrar u ocultar la pestaña Inventario de Apps (se recuerda) |
| `--genie-costs-tab` / `--no-genie-costs-tab` | Mostrar u ocultar la pestaña Costos de Genie (se recuerda) |
| `--check-sources` | Vuelve a comprobar el acceso a las system tables, por ejemplo después de que un administrador conceda nuevas tablas, y reactiva las reglas correspondientes |
| `--stop-on-missing` | Se detiene en lugar de desactivar las reglas cuyas tablas no se pueden leer |
| `--refresh` | Ejecuta ahora el pipeline y el tracker |
| `--bind-existing` | Adopta una app, un pipeline y un job instalados antes de que existiera el bundle |
| `--dry-run` | Ejecuta solo las verificaciones y la validación del bundle |

## Targets `dev` y `prod`

El target `dev` usa el modo de desarrollo del bundle. Los recursos reciben un prefijo en el nombre, la
programación queda pausada, el esquema es `brickpulse_dev` y la app se llama `brickpulse-dev`. Ambos
targets pueden convivir en el mismo workspace. `prod` se despliega en
`/Workspace/Users/<you>/.bundle/brickpulse/prod`, que es privado para el usuario que despliega y nunca
está en `/Shared`.

## Instalaciones anteriores al bundle

Si se instaló con el notebook antiguo `00_deploy_brickpulse`, usar `--bind-existing`. Así se vincula la
app, el pipeline y el job existentes (con los mismos nombres) al bundle, en lugar de crear duplicados.
Antes del primer despliegue, verificar que el plan contenga solo actualizaciones:

```sh
databricks bundle plan -t prod --profile <PROFILE>
```

## Desinstalar

```sh
databricks bundle destroy -t prod --profile <PROFILE>
```

Esto elimina la app, el pipeline y el job. Al eliminar el pipeline también se eliminan las tablas que
gestiona. El esquema y las tablas de historial del tracker (Delta simple) se conservan. Eliminar el
esquema manualmente cuando ya no se necesite el historial:

```sql
DROP SCHEMA <CATALOG>.brickpulse CASCADE;
```

Si algo sale mal, ver [solución de problemas](troubleshooting.md).
