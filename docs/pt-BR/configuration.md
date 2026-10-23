# Configuração

[English](../en/configuration.md) · [Español](../es/configuration.md) · **Português**

> Traduzido da versão em inglês. Em caso de divergência, a versão em inglês é a referência.

A maioria das instalações só define as opções expostas pelo `scripts/deploy.py`. Tudo abaixo é
opcional.

## Variáveis do bundle

Elas são declaradas no `databricks.yml`. O `scripts/deploy.py` as grava em
`.databricks/bundle/<target>/variable-overrides.json`. Com a CLI do bundle, passe-as como
`--var nome=valor`.

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `catalog` | — (obrigatória) | Catálogo existente do Unity Catalog para o schema do BrickPulse |
| `warehouse_id` | — (obrigatório) | SQL warehouse que o app consulta |
| `schema` | `brickpulse` (`brickpulse_dev` no `dev`) | Schema das tabelas do BrickPulse |
| `app_name` | `brickpulse` (`brickpulse-dev` no `dev`) | Letras minúsculas, dígitos e hífens, no máximo 30 caracteres |
| `lookback_days` | `30` | Dias de histórico das tabelas de sistema que cada atualização lê (1–90) |
| `pricing_mode` | `auto` | `auto`, `list` ou `account`; veja abaixo |
| `disabled_rules` | `none` | Ids de regras separados por vírgula para desativar. Definido pela verificação de fontes da implantação |
| `apps_tab` | `false` | Mostra a aba Inventário de Apps |
| `genie_costs_tab` | `false` | Mostra a aba Custos do Genie |

## Modo de preços

`pricing_mode` é uma escolha por instalação, não uma preferência por usuário:

| Modo | Comportamento |
| --- | --- |
| `auto` (padrão) | Usa `system.billing.account_prices` quando é legível e, caso contrário, os preços de lista |
| `list` | Nunca lê `account_prices`. Todas as estimativas usam preços de lista |
| `account` | Exige taxas contratuais legíveis cobrindo a janela de análise. Caso contrário, a atualização do pipeline falha |

As estimativas contratuais só contam quando SKU, nuvem, unidade, moeda e datas de vigência coincidem.
Para cada recurso, se menos de 95% do uso estiver coberto por taxas contratuais, a estimativa dele
continua em preço de lista, e o total contratual mostra esse fallback. O painel principal sempre
ranqueia por preços de lista, para que os resultados continuem comparáveis. As estimativas contratuais
são mostradas separadamente e não são valores de fatura.

Para mudar o modo, reimplante com o novo valor (`--pricing-mode`) e atualize. O pipeline e o app devem
usar o mesmo valor.

## Abas opcionais

| Recurso | Como ativar | Observações |
| --- | --- | --- |
| Inventário de Apps | `--apps-tab` | Lista os Databricks Apps e seus custos. Eventos de ciclo de vida, sozinhos, **não** provam que um app está ocioso, então nenhuma economia de app ocioso é atribuída a partir deles |
| Custos do Genie | `--genie-costs-tab` | Mostra os custos de uso dos espaços do Genie |
| *Perguntar ao Genie* | `BRICKPULSE_GENIE_ENABLED=true` mais um espaço | Desativado por padrão. Leia [segurança](security.md#genie) antes de ativar |

A implantação lembra cada escolha. Use `--no-apps-tab` ou `--no-genie-costs-tab` para desativar uma aba
novamente.

## Variáveis de ambiente

O bundle define estas variáveis no app (veja `resources/brickpulse.app.yml`). Você só precisa defini-las
manualmente ao executar o backend fora do bundle.

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `BRICKPULSE_COMPUTE_MODE` | `live` (`precompute` no bundle) | `precompute` lê as tabelas do pipeline; `live` executa o SQL das regras sob demanda; `demo` usa dados sintéticos |
| `BRICKPULSE_AUTH_MODE` | `obo` (`sp` no bundle) | `sp` = service principal do app com um mapa de escopo explícito por usuário; `obo` = somente persona de desenvolvimento |
| `BRICKPULSE_USER_SCOPE_JSON` | vazio (nega todos no modo `sp`) | Quem pode usar o app, e com qual escopo. Veja [segurança](security.md) |
| `BRICKPULSE_TABLE` | `main.finops.finops_recommendations` | Tabela de recomendações. As demais tabelas `finops_*` usam, por padrão, tabelas irmãs dela |
| `BRICKPULSE_CATALOG` | `system` | Catálogo que contém as tabelas de sistema |
| `BRICKPULSE_LOOKBACK_DAYS` | `30` | Janela de histórico |
| `BRICKPULSE_LIVE_LOOKBACK_DAYS` | `3` | Janela mais curta usada no modo `live` |
| `BRICKPULSE_PRICING_MODE` | `auto` | Deve coincidir com o pipeline |
| `BRICKPULSE_DISABLED_RULES` | vazio | Ids de regras separados por vírgula para desativar |
| `BRICKPULSE_HIDDEN_RULES` | `unused_scheduled_jobs,enable_photon_dbr` | Regras avaliadas, mas ocultas das listas principais |
| `BRICKPULSE_EXPERIMENTAL_RULES` | do registro de regras | Regras mostradas em Laboratório |
| `BRICKPULSE_PRECOMPUTE_CACHE_TTL` | `300` | Segundos que o app mantém em cache as leituras pré-computadas |
| `BRICKPULSE_PRECOMPUTE_CACHE_MAX_ROWS` | `10000` | Máximo de linhas agregadas mantidas nesse cache |
| `BRICKPULSE_APPS_TAB_ENABLED` | `false` | Aba Inventário de Apps |
| `BRICKPULSE_GENIE_COSTS_TAB_ENABLED` | `false` | Aba Custos do Genie |
| `BRICKPULSE_GENIE_ENABLED` | `false` | Painel *Perguntar ao Genie* |
| `BRICKPULSE_GENIE_SPACE_ID` / `BRICKPULSE_GENIE_SPACE_URL` | vazio | Espaço do Genie usado pelo *Perguntar ao Genie* |
| `BRICKPULSE_GENIE_MODE` | `iframe` | `iframe` incorpora o espaço; `api` usa o proxy da Conversation API |
| `DATABRICKS_WAREHOUSE_ID` | — | SQL warehouse (injetado pelo recurso do app) |
| `DATABRICKS_APP_PORT` | `8000` | Porta HTTP |
| `LOG_LEVEL` | `INFO` | Nível de log do Python |

Toda variável `BRICKPULSE_*` também aceita a grafia legada `FINOPS_*`. Quando as duas estão definidas,
`BRICKPULSE_*` prevalece. Um arquivo `.env` local é carregado, se existir. Ele está no `.gitignore`, então
nunca o versione.

## Limiares das regras

O que conta como desperdício fica em
[`src/brickpulse/rules/config/thresholds.yaml`](../../src/brickpulse/rules/config/thresholds.yaml). O
arquivo tem um bloco por id de regra, além de algumas configurações gerais do motor:

- `recency_days` (padrão 7): descarta achados em recursos sem uso cobrado nos últimos N dias.
- `min_observation_days` (padrão 5): o número mínimo de dias ativos distintos antes de qualquer regra
  poder recomendar uma ação.
- `enabled: false` dentro de um bloco de regra desativa essa regra.

Os limiares vêm dentro do wheel. Depois de editá-los, reimplante e atualize (`scripts/deploy.py ...
--refresh`). As estimativas de esforço ficam ao lado dos limiares, em `effort.yaml`.

## Idiomas e moeda

O idioma da interface segue o navegador, depois a escolha explícita do usuário e, por fim, o inglês.
Datas e números são localizados. Os valores estão sempre em USD, sem conversão de moeda. Os resumos das
regras são traduzidos. A evidência técnica completa permanece no inglês original, para que as ressalvas
dinâmicas continuem auditáveis.
