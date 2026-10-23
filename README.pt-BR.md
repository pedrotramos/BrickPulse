<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/brickpulse-lockup-color-dark.svg">
    <img src="docs/brand/brickpulse-lockup-color.svg" alt="BrickPulse" width="320">
  </picture>
</p>

<p align="center">
  <strong>FinOps para Databricks, somente leitura e orientado por evidências.</strong><br>
  Do gasto no nível da conta até a evidência por trás de cada oportunidade de economia.
</p>

<p align="center">
  <a href="https://github.com/pedrotramos/BrickPulse/actions/workflows/ci.yml"><img src="https://github.com/pedrotramos/BrickPulse/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776ab.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/node-20.19%2B-339933.svg" alt="Node 20.19+">
</p>

<p align="center">
  <a href="README.md">English</a> · <a href="README.es.md">Español</a> · <b>Português</b>
</p>

> Traduzido da versão em inglês. Em caso de divergência, a versão em inglês é a referência.

---

O BrickPulse é um [Databricks App](https://docs.databricks.com/dev-tools/databricks-apps/) que lê as
tabelas de sistema da conta, identifica onde o dinheiro está sendo desperdiçado e explica cada achado
com a evidência, o esforço envolvido e uma faixa de economia. Ele é **consultivo e somente leitura**:
nunca altera os recursos que analisa.

<p align="center">
  <img src="docs/assets/screenshot-overview.png" alt="Visão geral do BrickPulse: gasto observado, oportunidade estimada e economia acompanhada" width="900">
</p>

<details>
<summary>Mais capturas de tela</summary>

![Próximas ações ordenadas por economia estimada, esforço e prioridade](docs/assets/screenshot-next-best-actions.png)

</details>

## O que ele faz

- **Visão geral de gastos.** Gasto observado por workspace, dia e SKU, para os workspaces da região do
  metastore.
- **42 regras de custo.** As regras cobrem clusters, SQL warehouses, jobs, pipelines, model serving,
  inferência de IA, armazenamento e governança. Cada achado vem com evidência, faixa de estimativa,
  orientação passo a passo e um link para o recurso no Databricks.
- **Próximas melhores ações.** Os achados são ranqueados por economia estimada, esforço e prioridade.
  Quando várias opções se sobrepõem no mesmo recurso, elas são agrupadas em vez de somadas.
- **Análises.** Anomalias de custo, alocação de custos por tags e prontidão para serverless.
- **Acompanhamento de economias.** As economias são estimadas a partir das mudanças que o BrickPulse
  observa depois de uma recomendação. São estimativas, não conciliação de fatura.
- **Preços contratuais (opcional).** Estimativas extras a partir de `system.billing.account_prices`
  refletem descontos negociados. O preço de lista continua sendo a base comparável.
- **Superfícies opcionais.** Um inventário de Apps, custos do Genie e um painel *Perguntar ao Genie*.
  As três vêm desativadas por padrão.
- **Três idiomas.** A interface está disponível em inglês, espanhol e português do Brasil. Ela detecta o
  idioma do navegador, lembra uma escolha explícita e usa o inglês como alternativa. Os valores estão
  sempre em USD.

## Princípios de design

- **Todo número se explica.** Cada valor mostra seu período, fonte, confiança e atualização.
- **Desconhecido não é zero.** Dados ausentes nunca são apresentados como gasto zero ou como "nenhuma
  oportunidade".
- **O idioma não muda a conta.** Pontuação, ranking e valores não dependem do idioma da interface.
- **Recomenda, nunca age.** O BrickPulse não tem nenhum caminho de escrita nos recursos que analisa.
- **Menor privilégio.** O service principal do app recebe `SELECT` tabela por tabela, nunca em um schema
  inteiro. No início, só quem instalou o app pode abri-lo.

## Início rápido (demonstração local)

Você pode experimentar a interface completa com dados sintéticos, sem um workspace do Databricks. Você
precisa de Python 3.10+, Node 20.19+ e npm.

```sh
git clone https://github.com/pedrotramos/BrickPulse.git && cd BrickPulse
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
npm --prefix frontend ci
npm run dev        # depois abra http://127.0.0.1:5173
```

Os dados de demonstração são claramente identificados na interface e nunca acessam um workspace real.
Veja o [guia de primeiros passos](docs/pt-BR/getting-started.md) para mais detalhes.

## Implantação no seu workspace

O BrickPulse é distribuído como um [Declarative Automation Bundle](https://docs.databricks.com/dev-tools/bundles/).
Ele contém o app, um pipeline serverless e um job de atualização diária. Um único comando verifica o
acesso, implanta tudo, executa a primeira atualização e inicia o app:

```sh
.venv/bin/python scripts/deploy.py --profile <PROFILE> --target prod \
  --catalog <CATALOG> --warehouse-id <WAREHOUSE_ID>
```

Leia o [guia de implantação](docs/pt-BR/deployment.md) para ver os pré-requisitos, as concessões de
tabelas de sistema, as atualizações e a desinstalação.

## Documentação

| Guia | |
| --- | --- |
| [Primeiros passos](docs/pt-BR/getting-started.md) | Configuração local, modo demonstração, execução dos testes |
| [Implantação](docs/pt-BR/deployment.md) | Instalação em um workspace, atualizações, `dev` vs `prod`, desinstalação |
| [Configuração](docs/pt-BR/configuration.md) | Modo de preços, abas opcionais, variáveis de ambiente, limiares das regras |
| [Segurança](docs/pt-BR/security.md) | Identidades, escopo por usuário, dar acesso a mais pessoas |
| [Solução de problemas](docs/pt-BR/troubleshooting.md) | Grants ausentes, dados vazios, regras desativadas |

Referências para contribuidores (somente em inglês): [Arquitetura](docs/ARCHITECTURE.md),
[Modelo de dados](docs/DATA_MODEL.md), [Modelo de confiança das economias](docs/SAVINGS_CONFIDENCE_MODEL.md),
[Como adicionar uma regra](docs/ADDING_A_RULE.md), [Testes](docs/TESTING.md),
[Sistema de design](docs/DESIGN.md). O índice completo está em [docs/](docs/README.md) (em inglês).

## Estrutura do projeto

```text
frontend/src/        React app: app shell, features, components, lib, locales (en, es, pt-BR)
src/brickpulse/
  server/            FastAPI: API, identity, data sources, services, i18n, compiled UI (static/)
  rules/             42 rule plugins, Jinja SQL templates and thresholds
  pipelines/         Lakeflow precompute pipeline, change tracker and DDL
resources/           Bundle resources: app, pipeline, daily job (see databricks.yml)
scripts/             Dev server, deploy, wheel build, regression baseline
tests/               Python test suite and the frozen financial baseline
docs/                User guides (en, es, pt-BR) and contributor references
```

## Contribuindo

Contribuições são bem-vindas, sejam novas regras, correções de bugs, traduções ou documentação. Leia o
[CONTRIBUTING.md](CONTRIBUTING.md) (em inglês) para começar e siga o
[Código de Conduta](CODE_OF_CONDUCT.md) (em inglês). Reporte problemas de segurança em particular, como
descrito no [SECURITY.md](SECURITY.md) (em inglês).

## Licença

[Apache License 2.0](LICENSE) (em inglês). Veja o [NOTICE](NOTICE) para atribuições de terceiros.

O BrickPulse é um projeto open source independente. Ele não é afiliado, endossado nem patrocinado pela
Databricks, Inc. "Databricks" é uma marca registrada da Databricks, Inc.
