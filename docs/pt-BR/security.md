# Segurança e acesso

[English](../en/security.md) · [Español](../es/security.md) · **Português**

> Traduzido da versão em inglês. Em caso de divergência, a versão em inglês é a referência.

O BrickPulse lê dados de faturamento e de uso de uma região inteira do metastore, por isso importa quem
pode ver o quê. Esta página explica quais identidades existem, como o acesso é delimitado e como dar
acesso a mais pessoas. Para reportar uma vulnerabilidade, consulte [SECURITY.md](../../SECURITY.md) (em
inglês).

## Duas identidades separadas

A identidade que **executa o SQL** não é a identidade que **visualiza os dados**.

- As **consultas** rodam como o **service principal** do app (`BRICKPULSE_AUTH_MODE=sp`). Ele precisa de
  `CAN_USE` no warehouse e de `SELECT` nas tabelas do BrickPulse. A implantação concede `SELECT` tabela
  por tabela, nunca no schema inteiro. Quem visualiza não herda esses grants.
- Os **visualizadores** são identificados pelos cabeçalhos definidos pelo proxy autenticado do Databricks
  Apps (`x-forwarded-email`, `x-forwarded-preferred-username`, `x-forwarded-user`). Em seguida, o
  BrickPulse procura o usuário em um mapa de escopo explícito.

> [!CAUTION]
> Esses cabeçalhos só podem ser confiáveis atrás do proxy do Databricks Apps. Nunca exponha o backend
> diretamente em uma rede onde clientes possam definir cabeçalhos arbitrários.

O modo `obo` apenas simula uma persona de desenvolvimento. Ele não faz as consultas rodarem com os grants
do próprio visualizador, então nunca o use como autorização de produção.

## Quem pode abrir o app

Uma instalação nova é bloqueada em duas camadas:

1. **ACL do app.** Só o usuário que faz a implantação recebe `CAN_MANAGE` no app. Os administradores do
   workspace mantêm o acesso que herdam.
2. **Mapa de escopo.** `BRICKPULSE_USER_SCOPE_JSON` lista apenas o usuário que faz a implantação, como
   `platform_admin`.

O acesso falha de forma fechada. Um usuário que não está no mapa de escopo recebe HTTP 403 em todas as
rotas da API, incluindo configuração, regras e saúde dos dados. Um mapa vazio ou inválido nega acesso a
todos. `/health` é apenas uma sonda de vida do processo e não revela nada sobre dados ou fontes.

## Papéis e escopo de workspaces

| Papel | Vê |
| --- | --- |
| `platform_admin` | Todos os workspaces da região do metastore |
| `workspace_admin` | Somente os workspaces da lista `workspace_ids` |

O servidor deriva o escopo a partir da identidade do visualizador. Parâmetros de URL e o seletor de
workspace só podem **restringir** esse conjunto. Um `workspace_admin` que pedir um workspace fora da sua
lista recebe um resultado vazio, não acesso extra. Os ids de workspace usados nas consultas são validados
como numéricos, e os caches são indexados por host, warehouse, tabela e escopo.

Esse filtro acontece no app. **Não** é row-level security do Unity Catalog.

## Dar acesso a mais pessoas

Adicionar pessoas é uma mudança deliberada com duas partes. Faça as duas, porque cada uma sozinha não
basta.

1. Adicione os usuários ao mapa de escopo. Edite o valor de `BRICKPULSE_USER_SCOPE_JSON` em
   `resources/brickpulse.app.yml`:

   ```json
   {
     "owner@example.com":    {"role": "platform_admin"},
     "operator@example.com": {"role": "workspace_admin", "workspace_ids": ["1234567890123456"]}
   }
   ```

2. Conceda `CAN_USE` no app aos mesmos usuários. Adicione-os em `permissions` no mesmo arquivo ou use a
   página **Permissions** do app no workspace.

Depois, reimplante:

```sh
databricks bundle deploy -t prod --profile <PROFILE>
databricks bundle run brickpulse -t prod --profile <PROFILE>
```

## O que o app pode e não pode fazer

- Ele **lê** tabelas de sistema e as próprias tabelas `finops_*`. Não há nenhum caminho de código que
  altere clusters, warehouses, jobs ou qualquer outro recurso analisado.
- O SQL das regras interpola apenas identificadores validados (nomes de catálogo e de tabela). Valores
  derivados de usuários são passados como parâmetros ou validados como numéricos.
- O pipeline e o rastreador rodam como o usuário que faz a implantação, por meio do job do bundle.

## Genie

O *Perguntar ao Genie* vem desativado por padrão (`BRICKPULSE_GENIE_ENABLED=false`). No modo `api`, o
proxy precisa do token encaminhado do usuário e do escopo do Genie que a plataforma autoriza. O painel
mostra qual identidade respondeu, exibe o SQL gerado e os resultados, e avisa que as respostas são
geradas por IA. Antes de ativá-lo, revise os grants do espaço do Genie e o `user_api_scopes` do app.

## Nomes de configuração

As variáveis `BRICKPULSE_*` têm prioridade sobre os aliases legados `FINOPS_*`. Os padrões de
desenvolvimento não são uma configuração segura para produção. Use a configuração `sp` que o bundle
implanta.
