# Solução de problemas

[English](../en/troubleshooting.md) · [Español](../es/troubleshooting.md) · **Português**

> Traduzido da versão em inglês. Em caso de divergência, a versão em inglês é a referência.

Comece pela página **Saúde dos dados** do app. Ela mostra a última execução do pipeline, o quão atualizada
está cada fonte e quais fontes estão indisponíveis.

## A implantação para com "You can't read these required system tables"

Uma tabela de sistema obrigatória não pode ser lida pelo usuário que faz a implantação. Em português, a
mensagem significa que essas tabelas obrigatórias não podem ser lidas. O script imprime os comandos
`GRANT` que um **administrador do metastore** precisa executar. Se um schema de sistema ainda não estiver
habilitado, o administrador precisa [habilitá-lo](https://docs.databricks.com/admin/system-tables/)
primeiro. Execute a implantação novamente depois dos grants.

## Algumas regras estão desativadas

Uma tabela de sistema opcional não pôde ser lida durante a implantação, então as regras que dependem dela
foram desativadas. O script as lista (`N rule(s) switched off until their tables are readable: …`, ou
seja, "N regra(s) desativada(s) até que suas tabelas possam ser lidas"). Quando um administrador conceder
o acesso, execute:

```sh
.venv/bin/python scripts/deploy.py --profile <PROFILE> --target prod --check-sources --refresh
```

## "Could not grant the app read on system tables"

A verificação de saúde dos dados e a aba *Prontidão serverless* leem algumas tabelas de sistema em tempo
real, como o service principal do app. Só um administrador do metastore pode conceder esse acesso. O app
funciona sem ele, mas essas telas mostram as fontes como indisponíveis. Peça a um administrador que execute
os comandos impressos pelo script.

## O app abre, mas não mostra dados

- A primeira atualização pode ainda estar em execução. Verifique o job `[BrickPulse] Daily refresh` no
  workspace.
- Uma versão que alterou regras ou o pipeline precisa de uma atualização antes que os dados reflitam a
  mudança: `databricks bundle run daily_refresh -t prod --profile <PROFILE>`.
- Dados ausentes aparecem como *desconhecido*, nunca como zero. Se uma página disser que uma fonte está
  indisponível, verifique os grants dela em vez de interpretar isso como "sem gasto".

## Um workspace não aparece na visão geral

As tabelas de sistema cobrem apenas os workspaces da **região do metastore**. Um workspace em outra região
precisa de uma instalação própria, em um metastore dessa região.

## HTTP 403 para um colega

Esse usuário não está em `BRICKPULSE_USER_SCOPE_JSON` ou não tem `CAN_USE` no app. As duas coisas são
necessárias. Veja [dar acesso a mais pessoas](security.md#dar-acesso-a-mais-pessoas).

## "App deployed but not ready"

A mensagem significa que o compute do app, ou o próprio app, não chegou ao estado `RUNNING`. Abra o app no
workspace e verifique a aba **Logs**. Depois, inicie-o novamente:

```sh
databricks bundle run brickpulse -t prod --profile <PROFILE>
```

## "The frontend isn't built"

O arquivo `src/brickpulse/server/static/index.html` não existe, ou seja, o frontend não foi compilado.
Execute `npm --prefix frontend ci && npm run build` e depois implante novamente.

## Ainda com problemas?

Abra uma [issue](https://github.com/pedrotramos/BrickPulse/issues/new/choose). Inclua a versão do
BrickPulse, a sua nuvem, o target de implantação e a saída da implantação ou os logs do app. Oculte URLs
de workspace, e-mails e ids.
