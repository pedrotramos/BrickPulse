# Primeiros passos

[English](../en/getting-started.md) · [Español](../es/getting-started.md) · **Português**

> Traduzido da versão em inglês. Em caso de divergência, a versão em inglês é a referência.

Este guia configura o BrickPulse na sua máquina e o executa com dados sintéticos. Você não precisa de
um workspace do Databricks para nada disso. Para instalar o BrickPulse em um workspace, continue com o
[guia de implantação](deployment.md).

## Pré-requisitos

| Ferramenta | Versão | Usada para |
| --- | --- | --- |
| Python | 3.10 ou mais recente (os mantenedores usam a 3.13) | Backend, regras, pipeline, testes |
| Node.js | 20.19 ou mais recente | Build e servidor do frontend React |
| npm | Vem com o Node | Dependências do frontend |
| Git | Qualquer versão recente | Clonar o repositório |
| [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html) | Uma versão recente com suporte a bundles | Somente implantação |

## 1. Clonar e instalar

```sh
git clone https://github.com/pedrotramos/BrickPulse.git
cd BrickPulse

python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'

npm --prefix frontend ci
```

`.[dev]` instala as dependências de execução, além de pytest, httpx, ruff e `build`. O script de
implantação usa o `build` para empacotar o wheel.

## 2. Executar a demonstração

```sh
npm run dev
```

Abra <http://127.0.0.1:5173>. O modo de desenvolvimento executa o app FastAPI real em memória, com
**dados sintéticos**, e a interface identifica esses dados como demonstração. Os dados chegam ao
navegador por um transporte do Vite usado apenas em desenvolvimento. Ele não se conecta a nenhum
workspace, e nenhum desse código vai para o app implantado.

Use o menu de idioma para alternar entre inglês, espanhol e português.

## 3. Executar o backend isoladamente (opcional)

Para chamar a API diretamente, por exemplo com `curl` ou a partir de outro frontend:

```sh
PYTHONPATH=src BRICKPULSE_COMPUTE_MODE=demo .venv/bin/python -m brickpulse
```

O servidor escuta em `0.0.0.0` na porta `DATABRICKS_APP_PORT` (padrão `8000`) e serve a interface
compilada de `src/brickpulse/server/static`.

> [!WARNING]
> O modo demonstração e o modo de identidade de desenvolvimento confiam em padrões locais. Nunca os
> exponha em uma instalação de produção. Veja [segurança](security.md).

## 4. Executar as verificações

São as mesmas verificações que o CI executa:

```sh
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
npm --prefix frontend run typecheck
npm run build
```

Para os testes end-to-end no navegador, instale o Chromium uma vez:

```sh
(cd frontend && npx playwright install chromium)
npm --prefix frontend test
```

O [guia de testes](../TESTING.md) (em inglês) descreve o que cada suíte cobre.

## 5. Gerar o wheel (opcional)

```sh
npm run build
.venv/bin/python scripts/build_wheel.py
```

O build do React é servido pelo FastAPI e empacotado no wheel, então a produção nunca precisa do Node.
O script gera o build a partir de um diretório de staging limpo, para que arquivos antigos de builds
anteriores nunca entrem no pacote.

## Próximos passos

- [Implantar em um workspace](deployment.md)
- [Configurar preços, abas e regras](configuration.md)
- [Contribuir com uma mudança](../../CONTRIBUTING.md) (em inglês)
