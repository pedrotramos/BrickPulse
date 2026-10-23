# Validação da entrega — 21 de outubro de 2026

## Resultado local

- **124 testes Python aprovados**, com dois avisos de depreciação do ambiente de testes (Starlette/httpx e AnyIO). Os dois testes adicionais protegem o logo aprovado e seu uso consistente no app/favicon.
- Baseline financeiro congelado do projeto original: 213 achados sintéticos e 130 grupos visíveis; mesmos valores, IDs e ordenação.
- Os 42 templates renderizam com e sem fontes opcionais de preço; nove geradores de candidatos exercitam scoring real com três seeds.
- Testes de cache (TTL, LRU, peso, single-flight, retry), paginação/snapshot único, composição financeira, escopo e negação de usuários desconhecidos em produção.
- Paridade dos três catálogos e testes de 13 superfícies da API em português, inglês e espanhol, sem alterar números ou evidências originais.
- Manifests e notebook verificados localmente; preflight comparado aos SQLs das regras habilitadas e às dependências dos agregados analíticos.
- TypeScript e build de produção aprovados; compileall Python sem erros.

Esses testes não executam Spark nem as consultas contra system tables reais do workspace.

## Navegador conectado

O FastAPI real em modo demo respondeu via transporte local exclusivo de desenvolvimento.

- Navegação pelas 14 rotas principais e detalhe de recurso, sem alertas inesperados ou erros de console observados na rodada desktop.
- Português e espanhol, formatação localizada e idioma persistido após recarregar.
- Busca e seleção de workspace refletidas na URL; escopo preservado ao abrir o detalhe e nos links de navegação.
- Overview em viewport real de 390 × 844 sem overflow da página; tabelas mantêm regiões de rolagem próprias.
- Menu mobile: foco inicial, contenção com Tab/Shift+Tab, background inert, Escape e retorno do foco ao botão de abertura.

A revisão independente final aprovou **os dois ajustes avaliados**: contraste de metadados/rodapé e período da economia acumulada. Isso não é uma certificação de acessibilidade ou revisão exaustiva do produto. O sistema visual implementado está em DESIGN.md.

O Playwright lista dez cenários (cinco por viewport), mas **não foi executado com sucesso**: o ambiente restringe abertura do browser/socket. Na última tentativa, a inicialização do servidor falhou com `listen EPERM 127.0.0.1:5173`. As verificações no navegador conectado não são contabilizadas como testes Playwright aprovados.

### Logo aprovado: opção 3 original

Após avaliar seis propostas e rejeitar uma fusão de 3 e 4, o usuário escolheu a opção 3 original, **Frequência**. O app e o favicon utilizam o mesmo vetor de cinco barras arredondadas em `#B94B2C`, sem alterar a paleta petrol dos controles. A URL versionada evita reutilizar o ícone antigo em cache.

A igualdade com o vetor escolhido, mantido em `docs/brand/`, é protegida por teste. O build de produção foi refeito. A revisão independente aprovou somente a troca do logo, seu posicionamento e a ligação ao favicon, sem ajustes. Não é uma nova revisão completa do app. Os dois avisos de migração futura do React Router continuam presentes; nenhum erro de console foi observado nesta verificação.

## Empacotamento

O wheel deve ser gerado por `scripts/build_wheel.py` depois do build React; staging limpo evita assets antigos de `build/lib`. O deploy é feito pelo bundle (`databricks.yml`), que gera e envia o wheel como artifact; veja o README.

O wheel reconstruído traz os assets idênticos ao diretório estático atual, os três catálogos e os 42 templates de regras mais o macro compartilhado.

O lock Python completo reflete o ambiente local macOS/Python 3.13. As dependências diretas de produção são fixadas em requirements.txt; a instalação Linux e os transitivos ainda precisam de smoke test remoto.

## Deployment

A instalação é feita pelo bundle (seção Deployment do README). Os testes locais não substituem a validação no workspace de destino. Depois de instalar, confira:

- pipeline e baseline do tracker concluídos;
- ACL do app restrita a quem instalou;
- app RUNNING;
- identidade, escopo e dados reais;
- idiomas e freshness;
- fontes indisponíveis e regras desligadas, conforme impresso por `scripts/deploy.py`.

## Limitações explícitas

- Os 42 resumos de regras são traduzidos; instruções legadas dinâmicas completas e evidências técnicas permanecem no original em inglês, acessíveis em seção identificada.
- Cada tipo de evidência tem um gráfico próprio, com títulos, eixos e séries localizados. Textos livres gravados em inglês por linhas antigas aparecem traduzidos quando o padrão é reconhecido; os demais aparecem no original, marcados como inglês.
- Tabelas principais usam paginação no servidor; tabelas analíticas secundárias paginam agregados completos no cliente.
- USD não é convertido; economia acompanhada é estimativa baseada em mudanças, não conciliação de fatura.
- Ask Genie está desligado por padrão. Eventos de Apps não comprovam ociosidade. Nenhuma remediação automática foi adicionada.
