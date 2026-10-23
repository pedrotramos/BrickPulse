# Implantação

[English](../en/deployment.md) · [Español](../es/deployment.md) · **Português**

> Traduzido da versão em inglês. Em caso de divergência, a versão em inglês é a referência.

O BrickPulse é instalado em um workspace do Databricks como um
[Declarative Automation Bundle](https://docs.databricks.com/dev-tools/bundles/). O `databricks.yml` e a
pasta `resources/` declaram três recursos:

| Recurso | O que é |
| --- | --- |
| `brickpulse` app | O Databricks App (FastAPI + interface React compilada) |
| `precompute` pipeline | Um pipeline serverless do Lakeflow que avalia as regras e grava as tabelas `finops_*` |
| `daily_refresh` job | Executa o pipeline e, em seguida, o rastreador de economias, todos os dias às 05:00 UTC |

Nenhum valor de workspace é versionado, e não há target padrão. Você sempre escolhe o workspace com
`--profile` e o target (`dev` ou `prod`) de forma explícita.

## Pré-requisitos

**Na sua máquina**

- A configuração local de [primeiros passos](getting-started.md), incluindo o `.venv` com o pacote
  `build`.
- A [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html), autenticada no workspace
  de destino por meio de um profile (`databricks auth login --host <workspace-url> --profile <PROFILE>`).
- Um frontend compilado. O `src/brickpulse/server/static` é versionado, então você só precisa de
  `npm run build` depois de alterar o frontend.

**No workspace**

- Um **catálogo** do Unity Catalog onde você possa criar um schema. O BrickPulse cria `brickpulse` (ou
  `brickpulse_dev` para o target `dev`).
- Um **SQL warehouse** em que você tenha `CAN_USE`. As verificações da implantação e o app consultam
  por ele.
- Permissão para criar Databricks Apps, pipelines e jobs.
- **Acesso de leitura às tabelas de sistema** listadas abaixo, concedido a você, o usuário que faz a
  implantação. O pipeline roda como você.

### Tabelas de sistema

Obrigatórias. A implantação para se alguma delas não puder ser lida:

```text
system.billing.usage            system.billing.list_prices
system.access.audit             system.access.workspaces_latest
system.compute.clusters         system.compute.warehouses
system.lakeflow.jobs            system.lakeflow.pipelines
```

Opcionais. Se alguma delas não puder ser lida, a implantação desativa apenas as regras que dependem dela:

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

`system.billing.account_prices` passa a ser obrigatória com `--pricing-mode account`. Se você não tiver
acesso, a implantação imprime os comandos `GRANT` exatos que um administrador do metastore precisa
executar, por exemplo:

```sql
GRANT USE CATALOG ON CATALOG system TO `you@example.com`;
GRANT USE SCHEMA ON SCHEMA system.billing TO `you@example.com`;
GRANT SELECT ON TABLE system.billing.usage TO `you@example.com`;
```

> [!NOTE]
> As tabelas de sistema cobrem apenas os workspaces da **região do metastore**. O BrickPulse mostra o
> gasto da conta que ele consegue ver nessa região; não é uma visão de conta entre regiões.

## Primeira instalação

```sh
.venv/bin/python scripts/deploy.py --profile <PROFILE> --target prod \
  --catalog <CATALOG> --warehouse-id <WAREHOUSE_ID>
```

O script executa estas etapas:

1. Verifica o seu usuário, o catálogo e o warehouse.
2. Testa o acesso de leitura a cada tabela de sistema que usa. Se uma tabela obrigatória estiver
   faltando, para e imprime os `GRANT`s de que você precisa. Se uma tabela opcional estiver faltando,
   desativa as regras que dependem dela. Passe `--stop-on-missing` para parar também nesse caso.
3. Grava os valores escolhidos em `.databricks/bundle/<target>/variable-overrides.json`. Essa pasta fica
   fora do git, e as execuções seguintes reutilizam os valores.
4. Cria o schema.
5. Executa `databricks bundle deploy`.
6. Executa o pipeline e o rastreador uma vez. A primeira execução lê 30 dias de tabelas de sistema.
7. Concede ao service principal do app `SELECT` tabela por tabela, nunca no schema inteiro. Também tenta
   conceder leitura nas tabelas de sistema que o app consulta em tempo real. Isso exige um administrador
   do metastore. Se falhar, o app continua funcionando, e o script imprime os comandos para um
   administrador executar.
8. Inicia o app e imprime a URL dele.

No início, só você pode abrir o app (`CAN_MANAGE`), além dos administradores do workspace, que herdam o
acesso. Veja [segurança](security.md) para dar acesso a mais pessoas.

Execute primeiro com `--dry-run` para fazer apenas as verificações e a validação do bundle.

## Atualização

Implantar uma nova versão não executa nenhum job:

```sh
git pull
databricks bundle deploy -t prod --profile <PROFILE>
databricks bundle run brickpulse -t prod --profile <PROFILE>
```

Se a versão alterar regras, SQL ou o pipeline, os dados só refletem a mudança depois de uma atualização.
Execute o job de atualização agora ou espere a próxima execução diária:

```sh
databricks bundle run daily_refresh -t prod --profile <PROFILE>
```

`scripts/deploy.py --profile <PROFILE> --target prod --refresh` faz a implantação, a atualização e
reaplica os grants nas novas tabelas, tudo em uma etapa.

## Opções do script de implantação

| Flag | Efeito |
| --- | --- |
| `--profile` (obrigatório) | Profile da Databricks CLI do workspace de destino |
| `--target` (obrigatório) | `dev` ou `prod` |
| `--catalog`, `--warehouse-id` | Obrigatórios na primeira implantação em um target; depois são lembrados |
| `--schema` | Substitui o schema padrão de cada target |
| `--pricing-mode auto\|list\|account` | Fonte de preços. Veja [configuração](configuration.md#modo-de-preços) |
| `--lookback-days N` | Dias de histórico que cada atualização lê (1–90, padrão 30) |
| `--apps-tab` / `--no-apps-tab` | Mostra ou oculta a aba Inventário de Apps (lembrado) |
| `--genie-costs-tab` / `--no-genie-costs-tab` | Mostra ou oculta a aba Custos do Genie (lembrado) |
| `--check-sources` | Verifica de novo o acesso às tabelas de sistema, por exemplo depois que um administrador conceder novas tabelas, e reativa as regras correspondentes |
| `--stop-on-missing` | Para, em vez de desativar regras cujas tabelas não podem ser lidas |
| `--refresh` | Executa o pipeline e o rastreador agora |
| `--bind-existing` | Adota um app, pipeline e job instalados antes de o bundle existir |
| `--dry-run` | Executa apenas as verificações e a validação do bundle |

## Targets `dev` e `prod`

O target `dev` usa o modo de desenvolvimento do bundle. Os recursos recebem um prefixo no nome, o
agendamento fica pausado, o schema é `brickpulse_dev` e o app é `brickpulse-dev`. Os dois targets podem
coexistir no mesmo workspace. O `prod` é implantado em `/Workspace/Users/<you>/.bundle/brickpulse/prod`,
que é privado do usuário que faz a implantação e nunca fica em `/Shared`.

## Instalações anteriores ao bundle

Se você instalou com o notebook antigo `00_deploy_brickpulse`, passe `--bind-existing`. Ele vincula o
app, o pipeline e o job existentes (com os mesmos nomes) ao bundle, em vez de criar duplicatas. Antes
da primeira implantação, confira se o plano contém apenas atualizações:

```sh
databricks bundle plan -t prod --profile <PROFILE>
```

## Desinstalação

```sh
databricks bundle destroy -t prod --profile <PROFILE>
```

Isso remove o app, o pipeline e o job. Remover o pipeline também remove as tabelas que ele gerencia. O
schema e as tabelas de histórico do rastreador (Delta comum) permanecem. Apague o schema você mesmo
quando não precisar mais do histórico:

```sql
DROP SCHEMA <CATALOG>.brickpulse CASCADE;
```

Se algo der errado, veja [solução de problemas](troubleshooting.md).
