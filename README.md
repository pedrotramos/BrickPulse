# BrickPulse

Observabilidade financeira para Databricks: do gasto da conta às evidências de cada recurso. O app é consultivo e somente leitura — não aplica mudanças nos recursos analisados.

## O que faz

- Consolida gasto observado por workspace, dia e SKU na região do metastore.
- Avalia 42 regras de clusters, SQL warehouses, jobs, pipelines, serving, inferência, armazenamento e governança. Regras ocultas, experimentais ou desativadas preservam sua classificação original.
- Prioriza próximas ações por economia estimada, esforço e prioridade; agrupa opções sobrepostas sem simplesmente somá-las.
- Apresenta evidências, intervalos de estimativa e instruções, com links para o Databricks.
- Analisa anomalias de custo, alocação por tags e prontidão para serverless.
- Acompanha estimativas de economia a partir de mudanças observadas; não reconcilia faturas.
- Oferece inventário de Apps e custos de Genie opcionais. Eventos de ciclo de vida NÃO comprovam ociosidade de Apps.
- Mantém Ask Genie opcional, desligado por padrão.
- Pode calcular estimativas adicionais com `system.billing.account_prices`, refletindo
  descontos contratuais quando há correspondência de SKU, cloud, unidade, moeda e
  vigência. Exibe cobertura e mantém preço de lista como referência comparável.

Interface em português brasileiro, inglês e espanhol. Detecta o navegador, persiste a escolha explícita e usa inglês como fallback. Datas e números são localizados; valores continuam em USD, sem conversão. As 42 regras têm resumos traduzidos. O resultado original completo e as evidências técnicas permanecem disponíveis, em inglês, para preservar ressalvas dinâmicas e auditabilidade; não há tradução externa de dados.

## Desenvolvimento

### Fonte de preços

Na instalação, escolha `pricing_mode`: `auto` (padrão; usa `account_prices` quando
disponível), `list` (não consulta `account_prices`) ou `account` (exige acesso e
taxas na janela; falha se não houver). A escolha é da implantação, não uma
preferência por usuário. Para alterá-la, atualize o pipeline e republique o app
com o mesmo valor. Abaixo de 95% de cobertura por recurso, a estimativa desse
recurso permanece a preço de lista e o total contratual mostra o fallback.

O painel principal continua usando preços de lista para comparação consistente;
as estimativas contratuais aparecem separadamente. Não são valores finais de fatura.

Requer Python 3.10+ (validação local: 3.13), Node 20.19+ e npm.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
npm --prefix frontend ci
npm run dev
```

Abra http://127.0.0.1:5173. O modo de desenvolvimento usa dados sintéticos, identificados na tela, e o FastAPI real em memória através de um transporte exclusivo do Vite. Não acessa dados reais nem entra no runtime publicado.

Para executar o backend diretamente, instale as dependências e defina as variáveis de configuração, então:

```sh
PYTHONPATH=src BRICKPULSE_COMPUTE_MODE=demo python -m brickpulse
```

O servidor usa `0.0.0.0` e `DATABRICKS_APP_PORT` (8000 localmente). Não exponha o modo de demonstração/OBO de desenvolvimento como uma instalação de produção.

## Verificação e pacote

```sh
.venv/bin/python -m pytest -q
npm --prefix frontend run typecheck
npm run build
npm --prefix frontend test
.venv/bin/python scripts/build_wheel.py
```

O Playwright exige um navegador instalado (`npx playwright install chromium`). Neste ambiente, a execução desse navegador pelo shell foi bloqueada; os fluxos locais foram verificados com o navegador conectado. A suíte Python testa 42 templates, o baseline financeiro de 213 achados, agrupamentos, idiomas, cache e escopo; não substitui uma execução real dos SQLs/Spark no workspace.

`requirements.lock` captura o ambiente local Python 3.13, incluindo ferramentas de teste. `requirements.txt` fixa as dependências diretas de runtime; o Databricks resolve os transitivos compatíveis com Python/Linux. Não use o lock local como lock Linux universal.

## Estrutura

```text
frontend/src/
  app/          shell, navegação e roteamento
  features/     overview, opportunities, resource, analysis, settings
  components/   tabelas, estados e visualizações
  lib/          API, contratos, formatação e i18n
  locales/      pt-BR, en, es
src/brickpulse/
  server/       FastAPI, identidade, dados, serviços e apresentação
  rules/        42 plugins, SQL Jinja e configuração
  pipelines/    Lakeflow, change tracker e DDL
scripts/        desenvolvimento, regressão, wheel e deploy
resources/      app, pipeline e job do bundle (databricks.yml)
tests/          testes e baseline financeiro original
```

O build React é servido pelo FastAPI e incluído no wheel, sem exigir Node em produção. O script do wheel usa staging limpo para não reutilizar assets obsoletos de builds anteriores. Contratos `finops_*`, IDs e fórmulas foram mantidos para compatibilidade. Variáveis `BRICKPULSE_*` prevalecem sobre aliases legados `FINOPS_*`.

## Deployment

O BrickPulse é instalado como um [Declarative Automation Bundle](https://docs.databricks.com/dev-tools/bundles/): `databricks.yml` e `resources/` declaram o app, o pipeline serverless e o job diário (pipeline + tracker, 05:00 UTC). Nenhum valor de workspace fica versionado e não há target padrão: o workspace vem do `--profile` e o target (`dev` ou `prod`) é sempre explícito.

Pré-requisitos: Databricks CLI autenticada num perfil do workspace, um catálogo existente onde você possa criar schema, um SQL warehouse com CAN_USE e o frontend buildado (`npm run build`). O ambiente Python do projeto (`.venv`) precisa do pacote `build`, usado para gerar o wheel.

**Primeira instalação**

```sh
.venv/bin/python scripts/deploy.py --profile PERFIL --target prod --catalog CATALOGO --warehouse-id WAREHOUSE_ID
```

O script:

1. Confere usuário, catálogo e warehouse.
2. Testa a leitura de cada tabela de sistema usada. Se faltar uma obrigatória, para e imprime os `GRANT`s necessários. Se faltar uma opcional, desliga as regras que dependem dela; com `--stop-on-missing`, para em vez disso.
3. Grava os valores em `.databricks/bundle/<target>/variable-overrides.json` (fora do git).
4. Cria o schema.
5. Roda `bundle deploy`.
6. Executa o pipeline e o tracker uma vez.
7. Concede `SELECT` por tabela ao service principal do app, nunca no schema inteiro, e tenta conceder leitura nas tabelas de sistema que o app consulta (exige metastore admin; se falhar, imprime os comandos).
8. Publica o app.

O app fica acessível somente a quem instalou (CAN_MANAGE), além dos administradores herdados.

**Nova versão**

Publicar uma nova versão não roda nenhum job:

```sh
databricks bundle deploy -t prod --profile PERFIL
databricks bundle run brickpulse -t prod --profile PERFIL
```

Se a versão muda regras, SQL ou o pipeline, os dados só refletem a mudança depois de um refresh. Rode `databricks bundle run daily_refresh -t prod --profile PERFIL` ou espere o refresh diário. O mesmo vale para `scripts/deploy.py --profile PERFIL --target prod --refresh`, que também reaplica os grants de tabelas novas. As abas opcionais de inventário de Apps e de custos do Genie ficam desligadas por padrão; ligue-as com `--apps-tab` e `--genie-costs-tab`, ou desligue com `--no-apps-tab` e `--no-genie-costs-tab`. A escolha fica gravada para os próximos deploys. Use `--check-sources` depois que um admin conceder acesso a tabelas de sistema, para religar as regras correspondentes.

**Instalações anteriores ao bundle**

Para quem instalou com o antigo notebook `00_deploy_brickpulse`, `--bind-existing` vincula o app, o pipeline e o job existentes (mesmos nomes) ao bundle em vez de duplicá-los. Antes do primeiro deploy, revise `databricks bundle plan -t prod --profile PERFIL`: deve haver só updates.

O target `dev` usa o modo de desenvolvimento: recursos prefixados, schedule pausado, schema `brickpulse_dev` e app `brickpulse-dev`. Assim convive com o `prod` no mesmo workspace.

Veja [arquitetura e limites](docs/BRICKPULSE_ARCHITECTURE.md), [autenticação](docs/AUTH_AND_SCOPING.md) e [validação desta entrega](docs/VALIDATION.md). A documentação analítica legada continua em `docs/`; nomes físicos `finops_*` são intencionais.
