# Primeros pasos

[English](../en/getting-started.md) · **Español** · [Português](../pt-BR/getting-started.md)

> *Traducido de la versión en inglés. Si hay diferencias, la versión en inglés es la referencia.*

Esta guía configura BrickPulse en el equipo local y lo ejecuta con datos sintéticos. Para nada de esto
se necesita un workspace de Databricks. Para instalar BrickPulse en un workspace, ver la
[guía de despliegue](deployment.md).

## Requisitos previos

| Herramienta | Versión | Para qué se usa |
| --- | --- | --- |
| Python | 3.10 o más reciente (los mantenedores usan 3.13) | Backend, reglas, pipeline, pruebas |
| Node.js | 20.19 o más reciente | Compilar y servir el frontend en React |
| npm | Se incluye con Node | Dependencias del frontend |
| Git | Cualquier versión reciente | Clonar el repositorio |
| [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html) | Una versión reciente con soporte para bundles | Solo para el despliegue |

## 1. Clonar e instalar

```sh
git clone https://github.com/pedrotramos/BrickPulse.git
cd BrickPulse

python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'

npm --prefix frontend ci
```

`.[dev]` instala las dependencias de ejecución, además de pytest, httpx, ruff y `build`. El script de
despliegue usa `build` para empaquetar el wheel.

## 2. Ejecutar la demo

```sh
npm run dev
```

Abrir <http://127.0.0.1:5173>. El modo de desarrollo ejecuta la aplicación real de FastAPI en memoria,
con **datos sintéticos**, y la interfaz los identifica como datos de demostración. Los datos llegan al
navegador a través de un transporte de Vite que solo existe en desarrollo. No se conecta a ningún
workspace, y nada de este código se incluye en la app desplegada.

Para cambiar entre inglés, español y portugués, usar el menú de idioma.

## 3. Ejecutar el backend por separado (opcional)

Para llamar a la API directamente, por ejemplo con `curl` o desde otro frontend:

```sh
PYTHONPATH=src BRICKPULSE_COMPUTE_MODE=demo .venv/bin/python -m brickpulse
```

El servidor escucha en `0.0.0.0` en el puerto de `DATABRICKS_APP_PORT` (por defecto `8000`) y sirve la
interfaz compilada desde `src/brickpulse/server/static`.

> [!WARNING]
> El modo demo y el modo de identidad de desarrollo confían en valores locales por defecto. Nunca
> los exponga en una instalación de producción. Ver [seguridad](security.md).

## 4. Ejecutar las verificaciones

Son las mismas verificaciones que ejecuta CI:

```sh
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
npm --prefix frontend run typecheck
npm run build
```

Para las pruebas end-to-end en el navegador, instalar Chromium una sola vez:

```sh
(cd frontend && npx playwright install chromium)
npm --prefix frontend test
```

[Pruebas](../TESTING.md) (en inglés) describe qué cubre cada suite.

## 5. Compilar el wheel (opcional)

```sh
npm run build
.venv/bin/python scripts/build_wheel.py
```

La compilación de React la sirve FastAPI y se empaqueta en el wheel, así que producción nunca necesita
Node. El script compila desde un directorio de staging limpio, para que nunca se incluyan assets
obsoletos de compilaciones anteriores.

## Próximos pasos

- [Desplegar en un workspace](deployment.md)
- [Configurar precios, pestañas y reglas](configuration.md)
- [Contribuir con un cambio](../../CONTRIBUTING.md) (en inglés)
