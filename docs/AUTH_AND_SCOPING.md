# Autenticação e escopo do BrickPulse

## Identidades diferentes

A identidade que consulta SQL e a identidade que visualiza dados são separadas. O backend atual usa o PAT explícito local ou as credenciais do service principal do App para consultas SQL. O modo OBO legado simula persona de desenvolvimento; não deve ser interpretado como garantia de SQL executado com os grants do usuário.

A implantação BrickPulse usa `BRICKPULSE_AUTH_MODE=sp` e `BRICKPULSE_USER_SCOPE_JSON`. Os headers x-forwarded-email/preferred-username/user são confiáveis somente atrás do proxy autenticado de Databricks Apps. Nunca exponha esse backend diretamente à internet aceitando headers arbitrários.

## Acesso inicial

O notebook resolve seu usuário autenticado, configura somente ele como platform_admin e limita a ACL do App ao mesmo usuário. Administradores herdados da plataforma podem continuar com acesso administrativo. O service principal precisa CAN_USE no warehouse e SELECT nas tabelas de snapshot; esses grants não são repassados aos visitantes.

Usuários sem escopo válido recebem HTTP403 em todas as rotas API, inclusive config, regras e saúde. /health é apenas liveness do processo; não informa disponibilidade das fontes. Manifests não renderizados têm mapa vazio e negam acesso.

## Escopo por workspace

`Scope` é derivado da identidade no servidor. Parâmetros e rotas de workspace apenas restringem esse conjunto. Um workspace_admin pedindo outro workspace recebe conjunto vazio, não acesso adicional. IDs usados nas consultas são validados como numéricos. Cache de recomendações separa host, warehouse, tabela e escopo.

Exemplo de configuração controlada pelo administrador:

```json
{
  "owner@example.com": {"role": "platform_admin"},
  "operator@example.com": {"role": "workspace_admin", "workspace_ids": ["123"]}
}
```

Adicionar pessoas é uma mudança deliberada posterior; também requer revisar a ACL do App. Não confundir a filtragem do app com row-level security do Unity Catalog.

## Genie opcional

Ask Genie permanece desligado. O proxy existente, quando habilitado em modo API, exige token encaminhado do usuário e o escopo Genie autorizado pela plataforma. O painel informa a identidade, mostra SQL e resultados e avisa sobre respostas geradas por IA. Não habilite sem revisar os grants do espaço e user_api_scopes.

## Compatibilidade

Variáveis BRICKPULSE_* têm precedência; FINOPS_* são aliases legados. Os defaults de desenvolvimento não são uma configuração segura de produção. Use a configuração SP renderizada pelo fluxo de deployment.
