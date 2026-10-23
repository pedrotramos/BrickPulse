# Configuración

[English](../en/configuration.md) · **Español** · [Português](../pt-BR/configuration.md)

> *Traducido de la versión en inglés. Si hay diferencias, la versión en inglés es la referencia.*

La mayoría de las instalaciones solo configuran las opciones que expone `scripts/deploy.py`. Todo lo que
aparece abajo es opcional.

## Variables del bundle

Se declaran en `databricks.yml`. `scripts/deploy.py` las escribe en
`.databricks/bundle/<target>/variable-overrides.json`. Con la CLI del bundle, se pasan con
`--var nombre=valor`.

| Variable | Por defecto | Descripción |
| --- | --- | --- |
| `catalog` | — (obligatoria) | Catálogo de Unity Catalog existente para el esquema de BrickPulse |
| `warehouse_id` | — (obligatorio) | SQL warehouse a través del cual consulta la app |
| `schema` | `brickpulse` (`brickpulse_dev` en `dev`) | Esquema para las tablas de BrickPulse |
| `app_name` | `brickpulse` (`brickpulse-dev` en `dev`) | Letras minúsculas, dígitos y guiones; máximo 30 caracteres |
| `lookback_days` | `30` | Días de historial de system tables que lee cada actualización (1–90) |
| `pricing_mode` | `auto` | `auto`, `list` o `account`; ver más abajo |
| `disabled_rules` | `none` | IDs de reglas separados por comas para desactivar. Lo define la verificación de fuentes del despliegue |
| `apps_tab` | `false` | Muestra la pestaña Inventario de Apps |
| `genie_costs_tab` | `false` | Muestra la pestaña Costos de Genie |

## Modo de precios

`pricing_mode` es una elección por instalación, no una preferencia por usuario:

| Modo | Comportamiento |
| --- | --- |
| `auto` (por defecto) | Usa `system.billing.account_prices` cuando es legible; si no, usa precios de lista |
| `list` | Nunca lee `account_prices`. Todas las estimaciones usan precios de lista |
| `account` | Requiere tarifas contractuales legibles que cubran la ventana de análisis. Si no, la actualización del pipeline falla |

Las estimaciones contractuales solo cuentan cuando coinciden el SKU, la nube, la unidad, la moneda y las
fechas de vigencia. Para cada recurso, si menos del 95% de su uso está cubierto por tarifas
contractuales, su estimación se mantiene a precio de lista, y el total contractual muestra ese retroceso.
La vista principal (Resumen) siempre ordena con precios de lista para que los resultados sigan siendo
comparables. Las estimaciones contractuales se muestran por separado y no son importes de factura.

Para cambiar el modo, volver a desplegar con el nuevo valor (`--pricing-mode`) y actualizar. El pipeline
y la app deben usar el mismo valor.

## Pestañas opcionales

| Función | Cómo activarla | Notas |
| --- | --- | --- |
| Inventario de Apps | `--apps-tab` | Lista las Databricks Apps y sus costos. Los eventos de ciclo de vida por sí solos **no** demuestran que una app esté inactiva, así que no se reclama ahorro por apps inactivas a partir de ellos |
| Costos de Genie | `--genie-costs-tab` | Muestra los costos de uso de los Genie Spaces |
| Preguntar a Genie (Ask Genie) | `BRICKPULSE_GENIE_ENABLED=true` más un space | Desactivada por defecto. Leer [seguridad](security.md#genie) antes de activarla |

El despliegue recuerda cada elección. Para ocultar de nuevo una pestaña, usar `--no-apps-tab` o
`--no-genie-costs-tab`.

## Variables de entorno

El bundle configura estas variables en la app (ver `resources/brickpulse.app.yml`). Solo hace falta
definirlas manualmente cuando se ejecuta el backend fuera del bundle.

| Variable | Por defecto | Descripción |
| --- | --- | --- |
| `BRICKPULSE_COMPUTE_MODE` | `live` (`precompute` en el bundle) | `precompute` lee las tablas del pipeline; `live` ejecuta el SQL de las reglas al recibir cada solicitud; `demo` usa datos sintéticos |
| `BRICKPULSE_AUTH_MODE` | `obo` (`sp` en el bundle) | `sp` = service principal de la app con un mapa de alcance explícito por usuario; `obo` = solo persona de desarrollo |
| `BRICKPULSE_USER_SCOPE_JSON` | vacío (deniega a todos en modo `sp`) | Quién puede usar la app y con qué alcance. Ver [seguridad](security.md) |
| `BRICKPULSE_TABLE` | `main.finops.finops_recommendations` | Tabla de recomendaciones. Las demás tablas `finops_*` toman por defecto las tablas hermanas |
| `BRICKPULSE_CATALOG` | `system` | Catálogo que contiene las system tables |
| `BRICKPULSE_LOOKBACK_DAYS` | `30` | Ventana de historial |
| `BRICKPULSE_LIVE_LOOKBACK_DAYS` | `3` | Ventana más corta que se usa en modo `live` |
| `BRICKPULSE_PRICING_MODE` | `auto` | Debe coincidir con el del pipeline |
| `BRICKPULSE_DISABLED_RULES` | vacío | IDs de reglas separados por comas para desactivar |
| `BRICKPULSE_HIDDEN_RULES` | `unused_scheduled_jobs,enable_photon_dbr` | Reglas evaluadas pero ocultas de las listas principales |
| `BRICKPULSE_EXPERIMENTAL_RULES` | desde el registro de reglas | Reglas que se muestran en Laboratorio |
| `BRICKPULSE_PRECOMPUTE_CACHE_TTL` | `300` | Segundos que la app guarda en caché las lecturas precalculadas |
| `BRICKPULSE_PRECOMPUTE_CACHE_MAX_ROWS` | `10000` | Máximo de filas agregadas que se guardan en esa caché |
| `BRICKPULSE_APPS_TAB_ENABLED` | `false` | Pestaña Inventario de Apps |
| `BRICKPULSE_GENIE_COSTS_TAB_ENABLED` | `false` | Pestaña Costos de Genie |
| `BRICKPULSE_GENIE_ENABLED` | `false` | Panel Preguntar a Genie |
| `BRICKPULSE_GENIE_SPACE_ID` / `BRICKPULSE_GENIE_SPACE_URL` | vacío | Genie space para Preguntar a Genie |
| `BRICKPULSE_GENIE_MODE` | `iframe` | `iframe` incrusta el space; `api` usa el proxy de la Conversation API |
| `DATABRICKS_WAREHOUSE_ID` | — | SQL warehouse (se inyecta desde el recurso de la app) |
| `DATABRICKS_APP_PORT` | `8000` | Puerto HTTP |
| `LOG_LEVEL` | `INFO` | Nivel de log de Python |

Cada variable `BRICKPULSE_*` también acepta la forma heredada `FINOPS_*`. Si se definen ambas, gana
`BRICKPULSE_*`. Si existe, se carga un archivo `.env` local. Está ignorado por git, así que nunca debe
incluirse en un commit.

## Umbrales de las reglas

Lo que se considera desperdicio se define en
[`src/brickpulse/rules/config/thresholds.yaml`](../../src/brickpulse/rules/config/thresholds.yaml). Tiene
un bloque por cada ID de regla, además de algunos parámetros globales del motor:

- `recency_days` (por defecto 7): descarta hallazgos en recursos sin uso facturado en los últimos N días.
- `min_observation_days` (por defecto 5): mínimo de días activos distintos antes de que cualquier regla
  pueda recomendar una acción.
- `enabled: false` dentro de un bloque de regla desactiva esa regla.

Los umbrales se incluyen en el wheel. Después de editarlos, volver a desplegar y actualizar
(`scripts/deploy.py ... --refresh`). Las estimaciones de esfuerzo están junto a los umbrales, en
`effort.yaml`.

## Idiomas y moneda

El idioma de la interfaz sigue al navegador, luego a la elección explícita del usuario y, por último,
al inglés. Las fechas y los números se localizan. Los importes siempre están en USD, sin conversión de
moneda. Los resúmenes de las reglas están traducidos. La evidencia técnica completa se mantiene en
inglés, el idioma original, para que las advertencias dinámicas sigan siendo auditables.
