# Arquitetura e decisões

## Separação de responsabilidades

React organiza a interface por feature, usando TanStack Query para estados/cancelamento/cache de requisições, React Router para contexto de navegação e i18next para idiomas. FastAPI serve API e assets da SPA no mesmo processo. Uma instalação em produção não precisa Node.

O pacote Python único possui três domínios: server (HTTP/apresentação/fontes), rules (contrato/SQL/scoring puro) e pipelines (pré-cálculo e histórico). O wheel é compartilhado por pipeline e aplicação. Materialized views preservam a semântica batch; intermediários privados evitam publicar tabelas auxiliares. Sem migração destrutiva MV/ST ou full refresh.

## Finanças e contratos

USD é a unidade canônica. Idioma nunca altera scoring, ranking, deduplicação ou valores. O baseline original mantém 213 achados sintéticos e seus totais/agrupamentos. Isso não constitui validação real das 42 consultas nos system tables do destino.

O agrupamento e a composição financeira existentes foram preservados, inclusive regras ocultas, governance sidecar e Labs. O inventário de Apps não gera recomendações por suposta ociosidade. Histórico de economia é uma estimativa baseada em mudanças, com período explicitado, não uma fatura reconciliada.

## Internacionalização

Browser → seleção explícita persistida → fallback inglês. A API negocia Accept-Language com pesos e envia Content-Language/Vary. Localização atua numa cópia do JSON já serializado; não modifica objetos de cache nem snapshots persistidos.

Cada regra tem título, porquê e orientação resumidos nos três idiomas. Referências de mensagem versionadas e parâmetros numéricos seguem no JSON de evidência. Dados legados continuam legíveis. Instruções dinâmicas originais e evidências são exibidas numa seção identificada como original técnico em inglês. A tradução literal de cada frase dinâmica legada ainda não foi implementada; não se deve apresentar o resumo como tradução integral desse conteúdo.

As visualizações aceitam os contratos de evidência existentes, mostrando séries selecionáveis ou comparações numéricas com acesso aos dados brutos. São adaptadores genéricos, não réplicas de todos os gráficos bespoke do frontend compilado original.

## Otimizações

- Página de grupos obtém linhas e resumo do mesmo snapshot e omite evidências volumosas da listagem.
- Detalhe reutiliza uma leitura para as partições principal/governança/experimental.
- Cache LRU ponderado com TTL rejeita entradas grandes, limpa expiradas e coalesce misses concorrentes; falhas não são armazenadas.
- Cache de descoberta de colunas expira e volta a tentar após falha, preservando colunas de composição financeira antes descartadas.
- TTL padrão de pré-cálculo: 300s; limite 10.000 linhas agregadas no cache. Configure BRICKPULSE_PRECOMPUTE_CACHE_* conforme o volume.
- Módulos secundários e gráficos são divididos em chunks; fontes são locais.
- Tabelas principais têm paginação/filtros no backend; tabelas analíticas secundárias ainda recebem agregados completos e paginam 25 linhas no cliente.
- Evidências técnicas grandes limitam a renderização inicial de arrays a 100 elementos; a API preserva o conteúdo completo.

## Limites operacionais

Não há avaliação de latência/custo em um fleet real nem execução Spark local. Fontes são governadas por grants de sistema e pela região do metastore. A ausência de dados precisa ser distinguida de zero; verifique Data health e a última execução antes de decisões.

O arquivo requirements.lock é o ambiente local, não um lock universal. Transientes do runtime Linux ainda precisam da instalação e smoke test remotos. Cache é por processo, não distribuído. Paginação de grupos reduz payload e repetição de leituras, mas o agrupamento ainda acontece sobre o snapshot carregado no servidor.

O deploy inicial via notebook e o bundle app-only não devem controlar o mesmo recurso independentemente; vincule/import o recurso se migrar o controle.
