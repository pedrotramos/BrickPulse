# Seguridad y acceso

[English](../en/security.md) · **Español** · [Português](../pt-BR/security.md)

> *Traducido de la versión en inglés. Si hay diferencias, la versión en inglés es la referencia.*

BrickPulse lee datos de facturación y de uso de toda una región de metastore, así que importa quién puede
ver qué. Esta página explica qué identidades existen, cómo se limita el acceso y cómo dar acceso a más
personas. Para reportar una vulnerabilidad, ver [SECURITY.md](../../SECURITY.md) (en inglés).

## Dos identidades separadas

La identidad que **ejecuta el SQL** no es la misma que **ve los datos**.

- **Las consultas** se ejecutan como el **service principal** de la app (`BRICKPULSE_AUTH_MODE=sp`). Necesita
  `CAN_USE` en el warehouse y `SELECT` en las tablas de BrickPulse. El despliegue concede `SELECT` tabla
  por tabla, nunca sobre un esquema completo. Los usuarios que ven la app no heredan estos permisos.
- **Los usuarios que ven los datos** se identifican a partir de los encabezados que establece el proxy
  autenticado de Databricks Apps (`x-forwarded-email`, `x-forwarded-preferred-username`,
  `x-forwarded-user`). Después, BrickPulse busca al usuario en un mapa de alcance explícito.

> [!CAUTION]
> Estos encabezados solo son confiables detrás del proxy de Databricks Apps. Nunca exponer el backend
> directamente a una red en la que los clientes puedan definir encabezados arbitrarios.

El modo `obo` solo simula una persona de desarrollo. No hace que las consultas se ejecuten con los
permisos propios del usuario que ve los datos, así que nunca debe usarse como autorización de producción.

## Quién puede abrir la app

Una instalación nueva queda bloqueada en dos niveles:

1. **ACL de la app.** Solo el usuario que despliega recibe `CAN_MANAGE` sobre la app. Los administradores
   del workspace conservan el acceso que heredan.
2. **Mapa de alcance.** `BRICKPULSE_USER_SCOPE_JSON` lista solo al usuario que despliega, como
   `platform_admin`.

El acceso falla de forma cerrada. Un usuario que no está en el mapa de alcance recibe HTTP 403 en cada
ruta de la API, incluidas la configuración, las reglas y el estado de los datos. Un mapa vacío o inválido
deniega el acceso a todos. `/health` es solo una sonda de vida del proceso y no revela nada sobre datos
ni fuentes.

## Roles y alcance de workspaces

| Rol | Ve |
| --- | --- |
| `platform_admin` | Todos los workspaces de la región del metastore |
| `workspace_admin` | Solo los workspaces de su lista `workspace_ids` |

El servidor deriva el alcance de la identidad del usuario. Los parámetros de URL y el selector de
workspace solo pueden **reducir** ese conjunto. Un `workspace_admin` que pida un workspace fuera de su
lista recibe un resultado vacío, no acceso adicional. Los IDs de workspace que se usan en las consultas se
validan como numéricos, y las cachés se indexan por host, warehouse, tabla y alcance.

Este filtrado ocurre en la app. **No** es seguridad a nivel de filas de Unity Catalog.

## Dar acceso a más personas

Agregar personas es un cambio deliberado que tiene dos partes. Hay que hacer ambas, porque cada una por
separado no basta.

1. Agregar los usuarios al mapa de alcance. Editar el valor de `BRICKPULSE_USER_SCOPE_JSON` en
   `resources/brickpulse.app.yml`:

   ```json
   {
     "owner@example.com":    {"role": "platform_admin"},
     "operator@example.com": {"role": "workspace_admin", "workspace_ids": ["1234567890123456"]}
   }
   ```

2. Dar a los mismos usuarios `CAN_USE` sobre la app. Agregarlos en `permissions` en el mismo archivo, o
   usar la página **Permissions** de la app en el workspace.

Después, volver a desplegar:

```sh
databricks bundle deploy -t prod --profile <PROFILE>
databricks bundle run brickpulse -t prod --profile <PROFILE>
```

## Qué puede y qué no puede hacer la app

- **Lee** las system tables y sus propias tablas `finops_*`. No tiene ninguna ruta de código que modifique
  clústeres, warehouses, jobs ni ningún otro recurso analizado.
- El SQL de las reglas solo interpola identificadores validados (nombres de catálogo y de tabla). Los
  valores derivados de los usuarios se pasan como parámetros o se validan como numéricos.
- El pipeline y el tracker se ejecutan como el usuario que despliega, a través del job del bundle.

## Genie

*Preguntar a Genie* (Ask Genie) está desactivado por defecto (`BRICKPULSE_GENIE_ENABLED=false`). En modo
`api`, el proxy necesita el token reenviado del usuario y el scope de Genie que autoriza la plataforma.
El panel muestra qué identidad respondió, muestra el SQL generado y los resultados, y advierte que las
respuestas son generadas por IA. Antes de activarlo, revisar los permisos del Genie space y el
`user_api_scopes` de la app.

## Nombres de configuración

Las variables `BRICKPULSE_*` tienen prioridad sobre los alias heredados `FINOPS_*`. Los valores de
desarrollo por defecto no son una configuración segura para producción. Usar la configuración `sp` que
despliega el bundle.
