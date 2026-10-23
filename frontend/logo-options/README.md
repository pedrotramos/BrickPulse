# BrickPulse: seis propostas de logo

Estudos de identidade solicitados pelo usuário. **Opção aprovada: 3, Frequência.**
O app e o favicon utilizam o símbolo original da opção 3 em laranja `#B94B2C`.
A exploração abaixo fica preservada como histórico; a fusão de 3 e 4 foi rejeitada.

Visualização local com o servidor Vite existente:

<http://127.0.0.1:5173/logo-options/>

| Número | Conceito | Ideia |
| --- | --- | --- |
| 1 | Monograma BP | As iniciais como assinatura tipográfica. |
| 2 | Encaixe | Dois elos geométricos que se complementam. |
| 3 | Frequência | Amplitudes de um sinal amostrado. |
| 4 | Foco | Observabilidade e evidência no centro. |
| 5 | Fluxo | Um ciclo aberto de análise e otimização. |
| 6 | B Facetado | Uma letra esculpida, sólida e industrial. |

Os 36 SVGs estão em `svg/`: seis opções, cada uma com símbolo e assinatura
horizontal, nas versões colorida, monocromática e branca. As assinaturas incluem
a fonte Instrument Sans em WOFF2 embutido; os símbolos são geometria vetorial
sem dependências. A tipografia compartilhada permite comparar os símbolos.
SVGs brancos têm fundo transparente e devem ser visualizados sobre fundo escuro.
A licença SIL OFL 1.1 da fonte está em `FONT-LICENSE.txt` e nos metadados de
cada SVG que incorpora a fonte.

Regenerar os vetores e o catálogo a partir da arte declarada:

```sh
.venv/bin/python scripts/build_logo_options.py
```

Esta pasta não é uma entrada do build de produção. A galeria depende do Vite
para carregar o catálogo e a fonte local; abrir o HTML por `file://` não é o
fluxo suportado. Os SVGs individuais são portáveis.

Os nomes, formas e cores são propostas criativas, não uma pesquisa de marcas
registradas nem garantia de exclusividade. Após a escolha, finalizar proporções,
versão mínima e integração no aplicativo.

## Refinamento solicitado: 3 + 4

O usuário pediu uma combinação de Frequência (3) e Foco (4), mantendo o laranja
da opção 3, `#B94B2C`. A proposta **Foco + Frequência** preserva o contorno do olho
e substitui o losango por cinco barras. Está em `fusion/`, com seis SVGs próprios
(símbolo e assinatura nas três versões), fonte e licença embutidas como nas
opções originais. As seis opções originais permanecem inalteradas.

<http://127.0.0.1:5173/logo-options/fusion/>

O mesmo comando de geração acima produz os dois conjuntos. A fusão foi
rejeitada e não foi aplicada ao aplicativo. A escolha final é a opção 3 original.
