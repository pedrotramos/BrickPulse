# Solución de problemas

[English](../en/troubleshooting.md) · **Español** · [Português](../pt-BR/troubleshooting.md)

> *Traducido de la versión en inglés. Si hay diferencias, la versión en inglés es la referencia.*

Empezar por la página **Estado de los datos** (Data health) de la app. Muestra la última ejecución del
pipeline, qué tan reciente es cada fuente y cuáles no están disponibles.

## El despliegue se detiene con "You can't read these required system tables"

Una system table obligatoria no se puede leer para el usuario que despliega; es decir, no se pueden leer
las tablas del sistema obligatorias. El script imprime los `GRANT` que debe ejecutar un **administrador
del metastore**. Si un esquema de sistema todavía no está habilitado, el administrador primero tiene que
[habilitarlo](https://docs.databricks.com/admin/system-tables/). Volver a ejecutar el despliegue después
de los grants.

## Algunas reglas están desactivadas

Una system table opcional no se pudo leer durante el despliegue, así que se desactivaron las reglas que
dependen de ella. El script las lista con el mensaje `N rule(s) switched off until their tables are
readable: …` (N regla(s) desactivada(s) hasta que sus tablas sean legibles). Cuando un administrador
conceda el acceso, ejecutar:

```sh
.venv/bin/python scripts/deploy.py --profile <PROFILE> --target prod --check-sources --refresh
```

## "Could not grant the app read on system tables"

La comprobación de estado de los datos y la pestaña *Preparación serverless* leen en
vivo algunas system tables, usando el service principal de la app. Solo un administrador del metastore
puede conceder ese acceso. La app funciona sin él, pero esas vistas muestran las fuentes como no
disponibles. Pedir a un administrador que ejecute las sentencias que imprimió el script.

## La app abre pero no muestra datos

- La primera actualización puede seguir en ejecución. Revisar el job `[BrickPulse] Daily refresh` en el
  workspace.
- Una versión que cambió reglas o el pipeline necesita una actualización antes de que los datos la
  reflejen: `databricks bundle run daily_refresh -t prod --profile <PROFILE>`.
- Los datos faltantes se muestran como *desconocido*, nunca como cero. Si una página indica que una fuente
  no está disponible, revisar sus permisos en lugar de interpretarlo como "sin gasto".

## Falta un workspace en el Resumen

Las system tables solo cubren los workspaces de la **región del metastore**. Un workspace de otra región
necesita su propia instalación contra un metastore de esa región.

## HTTP 403 para un compañero

Ese usuario no está en `BRICKPULSE_USER_SCOPE_JSON`, o no tiene `CAN_USE` sobre la app. Se necesitan
ambas cosas. Ver [dar acceso a más personas](security.md#dar-acceso-a-más-personas).

## "App deployed but not ready"

La app se desplegó, pero el compute de la app, o la app misma, no llegó a `RUNNING`. Abrir la app en el
workspace y revisar su pestaña **Logs**. Luego, iniciarla de nuevo:

```sh
databricks bundle run brickpulse -t prod --profile <PROFILE>
```

## "The frontend isn't built"

Falta `src/brickpulse/server/static/index.html`, es decir, el frontend no está compilado. Ejecutar
`npm --prefix frontend ci && npm run build` y volver a desplegar.

## ¿Sigue sin resolverse?

Abrir un [issue](https://github.com/pedrotramos/BrickPulse/issues/new/choose). Incluir la versión de
BrickPulse, la nube, el target de despliegue y la salida del despliegue o los logs de la app. Ocultar las
URL de workspace, los correos electrónicos y los IDs.
