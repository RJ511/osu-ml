# Nomenclatura de skills da comunidade — perguntas e proposta de cálculo

Documento de discussão (passo 4→5→6 do `CLAUDE.md`). **Não implementa nada** — é a lista de
decisões que faltam tomar antes de traduzir a decomposição formal do jogo (`aim`, `speed`,
`flashlight`, já calculados em `difficulty.py`) para os termos que a comunidade usa (stream, jump,
stamina, reading, tech, ...). Cada pergunta vem com um comentário que propõe **exatamente** como
interpretar e calcular o termo a partir dos dados que já temos — para validar ou corrigir, não para
implementar às cegas.

Dados disponíveis para basear os cálculos: `hitobjects_<id>.parquet` (`beatmap_id`, `index`,
`time`, `end_time`, `x`, `y`, `kind`, `new_combo`, `combo_skip`, `hitsound`, `curve_type`,
`curve_points`, `slides`, `length`, `beat_length`, `sv`), `difficulty_<id>.parquet` (`stars`,
`aim`, `speed`, `flashlight`, `slider_factor`, `ar`, `hp`, ...), e séries temporais de
`Strains`/`GradualDifficulty` do `rosu-pp-py` (ainda **não** integradas no código — ver Q2.3).

## 0. Perguntas transversais (antes de entrar em cada skill)

**Q0.1 — Estamos a descrever o MAPA, o JOGADOR, ou os dois?**
A comunidade usa os mesmos termos ("este mapa é jump", "este jogador é bom em jump") para duas
coisas diferentes: uma propriedade do mapa (calculável só a partir do `.osu`) e uma capacidade do
jogador (inferida a partir do desempenho em mapas com essa propriedade). Proposta: nomear sempre
com prefixo — `map_<skill>_demand` (propriedade do mapa) vs. `player_<skill>_rating` (capacidade
inferida) — para nunca confundir as duas camadas no schema.

> ✅ **Resolução (utilizador)**: ambos. Para cada skill que se decida formalizar, calcular sempre
> os dois números — `map_<skill>_demand` e `player_<skill>_rating` — nunca só um. Justificação do
> utilizador: um mapa nunca é só uma habilidade, é sempre uma combinação; e um jogador, mesmo que
> se destaque numa skill, continua a ter as outras. Isto confirma a convenção de nomes com prefixo
> proposta acima como definitiva.

**Q0.2 — Sem replay, que atribuição por objeto é possível?**
A API só dá `statistics` agregadas por score (contagens de great/ok/meh/miss), não por hit object
nem com timestamp de erro. Não dá para dizer "o jogador falhou no 3.º objeto do stream". A única
ligação possível é correlacional: cruzar o outcome agregado do score (accuracy, miss count, pp,
`progress` num fail) com a composição de skills do mapa (calculada objeto a objeto a partir do
`.osu`, sem depender do jogador). Atribuição fina por objeto (ex.: "stream accuracy") precisaria de
replays `.osr`, fora do âmbito decidido (só osu!API v2).

> ✅ **Resolução (utilizador)**: confirmado — quando o replay não está disponível (é sempre o nosso
> caso, por decisão de só usar a osu!API v2), usar as propriedades do mapa (`map_<skill>_demand`)
> cruzadas com o outcome agregado do score para inferir `player_<skill>_rating`. Sem exceções a
> procurar mais tarde.

**Q0.3 — Quantas skills vale a pena formalizar já, com ~241 scores e ~40 tentativas recentes?**
Uma decomposição fina (>6 eixos) vai ter esparsidade extrema por combinação skill×nível. Proposta:
começar por 4 eixos largos — **Aim, Speed, Reading, Stamina** — que cobrem a maioria do vocabulário
da comunidade como combinações (ex.: "jump" = Aim alto + Reading baixo; "tech" = Speed médio +
Reading alto + irregularidade rítmica), e só refinar (jump vs flow, stream vs burst) mais tarde.
Consistente com "não fazer ainda: clustering de skills com ~40 tentativas recentes".

> ✅ **Resolução (utilizador)**: aceites os 4 eixos (Aim, Speed, Stamina, Reading). **Alerta do
> utilizador**: Reading é a mais difícil de precisar das quatro — não é um conceito único. Mistura
> pelo menos três coisas: (a) reading visual clássico (AR alto, ler o mapa a alta velocidade antes
> de o objeto aparecer todo), (b) reading de padrões/tech (perceber a estrutura do mapa e antecipar
> o que vem a seguir por familiaridade com o padrão, não por tempo de visualização) e (c) controlo
> do clique (execução motora sob a pressão de ter lido tarde ou mal). Isto é tratado na nova
> Q0.3b abaixo.

**Q0.3b — Como separar as três componentes de Reading sem se sobrepor a Tech (secção 4) e a
Accuracy (secção 6)?**
Proposta de fronteiras, a validar:
- **Reading visual** (3.1/3.2): proxy de AR/overlap/densidade — puramente do mapa, sem jogador.
  Fica como "Reading" propriamente dito.
- **Reading de padrões**: na prática coincide com o que a secção 4 já mede (entropia de snaps,
  irregularidade rítmica) — não é uma métrica nova, é a mesma métrica de Tech vista pelo ângulo de
  "quão previsível é o próximo objeto". Proposta: não duplicar — tratar `Tech` (4.1) como a
  componente de "reading de padrões", e `Reading` (3.1/3.2) como a componente "visual/AR". O eixo
  largo "Reading" do Q0.3 fica então definido como uma combinação de 3.1/3.2 (visual) + 4.1 (tech),
  não uma métrica única de raiz.
- **Controlo do clique**: isto é a componente de EXECUÇÃO, não de dificuldade do mapa — mede-se
  pelo desempenho do jogador (accuracy, misses) em mapas com Reading/Tech altos, ou seja, já é
  coberto pela secção 6 (`player_accuracy_rating` correlacionado com `map_reading_demand` e
  `map_tech_demand`). Não precisa de fórmula nova, precisa de análise cruzada quando houver dados.

> ✅ **Resolução (utilizador, ronda 5) — fórmula de combinação fechada**: `map_reading_demand` =
> **média quadrática (p-norm, p=2)** entre `visual` (3.1/3.2, normalizado 0–100 contra a pool de
> referência da Q0.4b) e `tech` (4.1, normalizado na mesma escala):
> `reading = sqrt((visual² + tech²) / 2)`.
> Dá mais peso ao componente mais difícil sem descartar o secundário — mesma filosofia do próprio
> jogo ao combinar `aim` e `speed` na estrela final (nem média simples, que dilui mapas extremos
> numa só dimensão, nem máximo puro, que perde a informação do componente secundário). Rejeitado:
> máximo (perde informação) e média ponderada simples (dilui extremos, pesos arbitrários). `p=2` é
> o valor de partida — revisitável mais tarde com mapas rotulados como referência, mas não bloqueia
> a implementação.
>
> **Dependência**: só se pode aplicar esta fórmula depois de `visual` e `tech` estarem na mesma
> escala normalizada (Q0.4b) — ou seja, a pool de referência (amostra + reservoir sampling) tem de
> existir primeiro.
>
> **Com isto, todos os pontos de design da Q0.1–Q0.4b e Q0.3b ficam fechados.** Próximo passo:
> implementar a extração por reservoir sampling (Q0.4b) para poder calcular as escalas 0–100.

**Q0.4 — Nível de mapa ou nível de secção?**
A comunidade descreve frequentemente mapas mistos ("é jump mas tem uma stream a meio"). Se
calcularmos só um valor por mapa perdemos exatamente essa informação. Proposta: calcular por
secção de tempo fixa (ex.: janelas de 400 ms, o mesmo grão que o `Strains` do rosu-pp já usa) e só
agregar para "o mapa" com estatísticas (média, máximo, % do tempo acima de um threshold).

> ✅ **Resolução (utilizador, ronda 2)**: híbrido. Calcular por secção de tempo sempre que os dados o
> permitam (guardar a série), **mas produzir sempre, no mínimo, um valor agregado por mapa e por
> skill** — mesmo quando o cálculo por secção não for possível ou não fizer sentido para essa
> skill. Esse valor agregado deve ser numa escala interpretável (o utilizador deu como exemplo:
> "Aim 60 [ou B+], Speed 50 [ou B-]"), não o número bruto da fórmula (ex.: `aim=2.7` do rosu-pp não
> diz nada por si só a um jogador). Estes valores agregados por mapa, para todas as skills, são o
> que se usa depois para (1) analisar o jogador e (2) tentar prever desempenho futuro — ou seja,
> são o input direto da fase de baselines (passo 7 do roadmap), não só uma curiosidade descritiva.
>
> Isto abre uma pergunta nova, ainda por resolver:
>
> **Q0.4b — Que escala usar para o valor agregado (0–100? letra tipo B+/B-? percentil?) e como
> normalizar valores brutos muito diferentes entre si (aim ~0–6, speed ~0–5, entropia de snap em
> bits, etc.) nessa escala?**
> Candidatos, a validar com o utilizador:
> 1. **Percentil relativo ao próprio jogador** (mais simples já com os dados atuais): "Aim 60" =
>    "mais difícil em Aim do que 60% dos 216 mapas que este jogador já jogou". Fácil de calcular
>    já, mas o valor não é comparável entre jogadores diferentes nem estável se o jogador começar a
>    jogar mapas de outro estilo.
> 2. **Percentil relativo a um pool de referência maior** (mais próximo do que a comunidade
>    entende por "B+" num mapa): precisaria de calcular a dificuldade de muito mais mapas do que os
>    216 jogados — já temos o dump inteiro (`2026_09_01_osu_files.tar.bz2`, ~235 mil `.osu`
>    escaneados no import) na máquina, por isso é tecnicamente possível correr `maps difficulty`
>    sobre uma amostra grande do dump (não só os mapas do jogador), mas é um aumento de âmbito e
>    tempo de cálculo que ainda não foi decidido.
> Proposta: começar pela opção 1 (mais simples, já calculável), documentar claramente que a escala
> é relativa ao próprio jogador (não uma nota objetiva de comunidade), e só avançar para a opção 2
> se/quando isso for necessário.
>
> ✅ **Resolução (utilizador, ronda 3)**: opção 2 (pool de referência maior), mas via **amostra
> grande** (5 000–10 000 mapas) em vez do dump inteiro (~235 mil), depois de orçamentar tempo/disco:
> - Dump inteiro: ~20–30 min de processamento total (extração ~15–25 min, cálculo de dificuldade
>   nomod ~4–31 min consoante paralelização) + 3–6 GB de disco permanentes em `data/raw/osu_files/`.
> - Amostra de 5–10 mil: cálculo de dificuldade cai para segundos; a extração não encolhe tanto
>   (o `.tar.bz2` tem de ser lido sequencialmente até ao fim de qualquer forma para ter uma amostra
>   espalhada por vários níveis), mas grava muitíssimos menos ficheiros a disco.
> - Rejeitado: dump inteiro (demasiado âmbito/disco agora) e "adiar" (o utilizador prefere avançar
>   já para uma escala mais próxima da comunidade do que só o repertório do próprio jogador).
>
> **Estratégia de amostragem (utilizador, ronda 4)**: **reservoir sampling** — amostragem aleatória
> uniforme numa única passagem pelo `.tar.bz2` em streaming, sem precisar de saber o total de
> ficheiros antecipadamente e sem viés de posição no dump. Rejeitado: estratificar por
> `difficulty_rating` (não temos esse valor da API para mapas fora dos 216 do jogador, e ir buscá-lo
> exigiria muitos pedidos extra à API, contra a política de minimizar pedidos — só seria viável
> calcular o SR localmente DEPOIS de já extraídos, o que inverte a ordem do processo); "primeiros N
> encontrados" (viés de ordenação do dump, provavelmente correlacionado com época de criação do
> mapa, não com dificuldade/estilo).
>
> **Q0.4b: totalmente fechada.** Falta só implementar: extensão a `import_from_path`/CLI para um
> modo "amostra aleatória do dump" (distinto do modo atual, que só importa mapas referenciados em
> scores) + correr `maps difficulty` (nomod) sobre essa amostra + calcular os percentis de
> referência a partir dela.

**Q0.5 — Thresholds fixos (canónicos) ou relativos (percentis do próprio dataset)?**
Com 216 mapas de 1 jogador, thresholds relativos arriscam sobreajustar ao repertório dele;
thresholds absolutos precisam de validação externa. Proposta: usar primeiro valores de referência
publicados pela comunidade/ferramentas existentes quando existirem (ex.: convenções conhecidas de
"jump" em unidades de raio do circle), e só afinar com percentis se não houver nada canónico.

> ✅ **Resolução (utilizador)**: concorda com a proposta.

**Q0.6 — Guardar onde?**
Proposta: nunca mexer em `hitobjects_<id>.parquet` (dados crus do parser). As features de skill
derivadas vão para um ficheiro novo (ex.: `skills_<id>.parquet`), mantendo a separação já usada no
projeto entre dados crus e camadas de features.

> ✅ **Resolução (utilizador)**: concorda com a proposta.

## 1. Aim

> ✅ **Resolução (utilizador)**: secção aprovada sem alterações (1.1, 1.2, 1.3, incluindo os
> thresholds relativos de Q1.3).

### 1.1 Aim "genérico" (já calculado, `difficulty.aim`)
**Q1.1 — Para que serve `aim` vs `aim_no_sliders` (ambos existem no `Strains` do rosu-pp)?**
A diferença isola o contributo dos sliders. Proposta: guardar as duas séries e o rácio
`aim / aim_no_sliders` como proxy de "quanto do aim vem de sliders" — distingue estilo "aim de
circles" (jump puro) de "aim de sliders" (flow com sliders longos).

### 1.2 Jump Aim / Sharp Aim
Definição da comunidade: sequências com saltos largos entre objetos consecutivos e mudanças de
ângulo acentuadas (>90°), tipicamente em circles, a BPM moderado.

**Q1.2 — Como medir "salto largo" e "ângulo acentuado" a partir de `x`, `y`, `time`, `cs`?**
- Distância normalizada: `dist_i = sqrt((x_i-x_{i-1})² + (y_i-y_{i-1})²) / raio_circle(cs)`
  (unidade "raios de circle", como a própria comunidade mede jumps).
- Tempo entre objetos: `dt_i = time_i - time_{i-1}` (ms); velocidade = `dist_i / dt_i`.
- Ângulo: para 3 objetos consecutivos, ângulo entre os vetores (i-2→i-1) e (i-1→i). >90° ≈
  anti-flow/"sharp"; <45° ≈ flow (ver 1.3).
- Candidato a "secção de jump": janelas onde `dist_i` normalizado excede um threshold (ver Q0.5) e
  o objeto não pertence a uma stream (ver 2.1).

**Q1.3 — Que threshold de distância separa "jump" de movimento normal?**
Não há valor canónico universal. Proposta: calibrar por percentis da distribuição de `dist_i`
normalizada dentro do próprio mapa (ex.: top 25% mais distante = "secção de jump"), documentando
que é relativo ao mapa, não um limiar absoluto — reavaliar com mais mapas (Q0.5).

### 1.3 Flow Aim
Definição: trajetória que segue o momentum do cursor (ângulos pequenos, muitas vezes guiada por
sliders), oposto de jump/sharp.

**Q1.4 — Como distinguir "flow" de apenas "aim lento"?**
Flow não é sobre velocidade, é sobre suavidade angular. Proposta: variância do ângulo (1.2) numa
janela deslizante — baixa variância + ângulos pequenos = flow, independentemente da velocidade.

## 2. Speed

> ✅ **Resolução (utilizador)**: secção aprovada sem alterações (2.1–2.5).

### 2.1 Stream
Definição: sequência longa (tipicamente ≥5 notas) de circles em snap curto e constante (1/4 a BPM
alto), com distância pequena entre objetos.

**Q2.1 — Como detetar a partir de `time`, `beat_length`, `sv`?**
- Snap de cada intervalo: `dt_i / beat_length` (fração do tempo de batida).
- Stream candidata: sequência consecutiva com `dt_i` aproximadamente constante (mesma fração,
  tipicamente 1/4, por vezes 1/3 ou 1/6), comprimento mínimo configurável (ver Q2.2), maioria
  `kind == "circle"`, distância pequena e regular entre objetos.

**Q2.2 — Onde fica a fronteira Stream vs. Burst?**
Mesma deteção; "burst" = sequência curta (3–6 notas) isolada, "stream" = sequência longa
(>8–10 notas) sustentada. O número exato é escolha de nomenclatura, não medida objetiva. Proposta:
guardar o **comprimento da sequência como número contínuo** e só rotular "stream"/"burst" como
categoria derivada de um threshold configurável — não perder a informação contínua atrás do rótulo.

### 2.2 Stamina
Definição: capacidade de manter velocidade alta ao longo de MUITO tempo, distinta de um pico
pontual (um burst curto não exige stamina mesmo que seja rápido).

**Q2.3 — Como operacionalizar "sustentado no tempo"?**
Precisa da série temporal `Strains.speed` do rosu-pp (`section_length` ms por ponto) —
**ainda não integrada no código** (só `DifficultyAttributes` agregados estão implementados em
`difficulty.py`; `Strains`/`GradualDifficulty` ficam para quando isto avançar). Proposta de
métrica: proporção do tempo do mapa acima de um percentil alto do próprio speed-strain do mapa, ou
área sob a curva de speed-strain acima de um threshold, normalizada pela duração — mede forma da
curva, não o pico.

**Q2.4 — Stamina do MAPA ou do JOGADOR?**
São coisas diferentes. Stamina do mapa = métrica acima (Q2.3). Stamina do jogador seria
"degradação de desempenho ao longo do mapa" (ex.: mais misses na 2.ª metade de mapas longos e
sustentados) — precisaria de dados por tempo dentro do score, que não existem sem replay (Q0.2). O
proxy mais próximo sem replay: comparar `progress` em fails de mapas com stamina alta vs. baixa.

### 2.3 Speed Jump vs. Aim Jump
Definição da comunidade: "speed jump" = saltos moderados mas muito rápidos (1/2–1/4 snap,
distância média); "aim jump" = saltos largos a ritmo mais lento (1/1–1/2), onde o desafio é a
distância/precisão, não a velocidade de tap.

**Q2.5 — Como separar os dois com `dist_i` e `dt_i`?**
Proposta: "intensidade de speed" = `1/dt_i` (notas/segundo); "intensidade de aim" = `dist_i`
normalizada (1.2). Um objeto é "speed jump" se `dt_i` pequeno domina, "aim jump" se `dist_i`
domina. Alternativa mais simples: usar diretamente `aim` vs. `speed` já calculados por mapa em
`difficulty.py` como proxy agregado, e só descer ao nível de objeto se isso não bastar.

### 2.4 Finger Control (Jacks / Trills)
Definição: repetição rápida na mesma posição (jacks) ou alternância entre duas posições próximas
(trills) — exige controlo independente dos dedos mais do que aim.

**Q2.6 — Como detetar via `x`, `y`, `time`?**
Jack = `dist_i` quase 0 (mesma posição) com `dt_i` curto repetido; trill = alternância entre 2
posições fixas com `dt_i` curto e constante. Proposta: tratar como subcategoria de stream (2.1)
filtrada por `dist_i` muito baixo — não precisa de deteção separada de raiz.

### 2.5 Alt / Singletap
**Q2.7 — Isto é sequer uma skill do MAPA?**
Não. É uma escolha de técnica de input do jogador (que dedos/padrão usa), não uma propriedade do
`.osu`. Não calcular a partir de hit objects — só seria possível com dados de input/replay, fora do
âmbito atual.

## 3. Reading

> ✅ **Resolução (utilizador)**: secção aprovada sem alterações — mas ver Q0.3b acima: "Reading",
> como eixo largo do Q0.3, é esta secção (3.1/3.2, componente visual) **combinada** com a secção 4
> (Tech, componente de padrões), não só isto sozinho.

### 3.1 Reading genérico (densidade visual / overlaps)
Definição: dificuldade em antecipar objetos por sobreposição visual, AR baixo face à densidade, ou
padrões pouco intuitivos.

**Q3.1 — Como medir "overlap" a partir de `x`, `y`, `time` e `ar` (de `difficulty.py`) sem simular
o jogo?**
Precisa da fórmula oficial AR→tempo de aproximação (`preempt_ms`), que ainda **não está
implementada** no projeto:
`preempt_ms = 1200 + 600*(5-AR)/5` se AR<5; `1200` se AR=5; `1200 - 750*(AR-5)/5` se AR>5.
Proposta: para cada par (i, j) com `time_j - time_i < preempt_ms`, marcar sobreposição se
`dist(i,j) < 2×raio_circle`. A proporção de objetos com ≥1 sobreposição na sua própria janela de
preempt é o proxy de densidade de reading.

**Q3.2 — Como incorporar o mod HD (visibilidade reduzida)?**
A fórmula de Q3.1 não muda com HD (o objeto some antes do fim do preempt). Proposta: calcular duas
versões — reading "nomod" e reading "HD" (janela de visibilidade reduzida conforme a fórmula do
próprio HD). Fica como refinamento, não bloqueante para uma primeira versão.

### 3.2 Slider Reading / Leniency
Definição: dificuldade de confiar no timing do fim de um slider sem olhar, ou ler curvas
complexas.

**Q3.3 — Que proxy usar a partir de `curve_type`, `curve_points`, `slides`, `length`?**
Proposta: pontos de controlo por unidade de comprimento (curvas "densas") e número de `slides`
(repeats) como proxy de complexidade — não substitui uma medida visual real, mas é o que dá para
calcular sem simulação gráfica.

## 4. Tech / Técnico

> ✅ **Resolução (utilizador)**: secção aprovada sem alterações. Nota (ver Q0.3b): esta secção
> passa a ser também a componente "reading de padrões" do eixo largo Reading, não fica isolada.

### 4.1 Tech (ritmo irregular)
Definição: padrões rítmicos pouco previsíveis — mistura de snaps 1/2, 1/3, 1/4, 1/6, mudanças de
BPM, trocas abruptas de padrão.

**Q4.1 — Como quantificar "irregularidade rítmica" a partir de `time` e `beat_length`?**
Proposta: calcular o snap de cada intervalo (fração de `beat_length`, como em 2.1) e medir a
**entropia** da distribuição de snaps num mapa/janela. Mapas "tech" têm entropia alta (muitos
snaps diferentes); mapas de stream ou jump puro têm entropia baixa (um snap domina). É
provavelmente a métrica mais direta de "tech" calculável sem julgamento subjetivo.

**Q4.2 — Mudanças de BPM/timing points contam como tech?**
Proposta: sim — `n_uninherited` (timing points não-herdados, já calculado no parser) por minuto de
mapa como proxy adicional de instabilidade rítmica.

## 5. Flashlight — ❌ excluído por decisão do utilizador

> ❌ **Resolução (utilizador)**: ignorar esta secção. Flashlight fica fora do sistema de skills por
> agora (o campo `difficulty.flashlight` continua a existir nos dados, simplesmente não entra em
> nenhuma tradução para nomenclatura de comunidade nem em `map_<skill>_demand`/
> `player_<skill>_rating`). Não avançar com Q5.1.

## 6. Accuracy / Consistência (skill do JOGADOR, não do mapa)

> ✅ **Resolução (utilizador)**: secção aprovada sem alterações. Passa também a ser o local onde se
> mede a componente "controlo do clique" de Reading (ver Q0.3b).

**Q6.1 — Vale a pena incluir isto no mesmo sistema de "skills"?**
A comunidade fala de "boa accuracy" como skill separada de aim/speed, mas ao contrário das
anteriores não é uma propriedade do mapa — já está diretamente nos dados (`accuracy`,
`statistics`). Proposta: não "calcular" a partir de hit objects; relacionar diretamente com as
skills do mapa (ex.: "a accuracy deste jogador cai mais em mapas de reading alto do que em mapas de
aim puro").

## 7. Tabela-resumo (validada, ver resoluções acima)

| Termo comunidade | Eixo largo (Q0.3) | Camada | Dados já disponíveis | Em falta | Skill formal relacionada |
|---|---|---|---|---|---|
| Aim (jump/flow/sharp) | Aim | mapa+jogador | hitobjects (x,y,time), difficulty.aim | thresholds (Q1.3) | aim |
| Stream / Burst | Speed | mapa+jogador | hitobjects (time, beat_length) | threshold de comprimento (Q2.2) | speed |
| Stamina | Stamina | mapa+jogador | — | integrar `Strains`/`GradualDifficulty` (Q2.3) | speed |
| Speed jump vs. Aim jump | Aim/Speed | mapa+jogador | hitobjects + difficulty (aim, speed) | — | aim + speed |
| Finger control (jacks/trills) | Speed | mapa+jogador | hitobjects (x,y,time) | — (subcaso de stream) | speed |
| Alt / Singletap | (fora do sistema) | jogador (técnica) | — | replay/input, fora de âmbito | — |
| Reading visual/overlap | Reading | mapa+jogador | hitobjects, difficulty.ar | fórmula AR→preempt_ms (Q3.1) | — |
| Slider reading | Reading | mapa+jogador | hitobjects (curve_*, slides) | — | aim |
| Tech (ritmo irregular) | Reading (padrões) | mapa+jogador | hitobjects (time), n_uninherited | entropia de snap (Q4.1) | speed + aim |
| Controlo do clique | Reading (execução) | jogador | scores.accuracy, statistics | correlação com map_reading/map_tech | (accuracy) |
| Accuracy / consistência | (transversal) | jogador | scores.accuracy, statistics | — | (já é direta) |
| ~~Flashlight~~ | — | — | — | **excluído por decisão do utilizador** | — |

Nota: "mapa+jogador" reflete a resolução de Q0.1 — todas as linhas ganham, quando implementadas,
um `map_<skill>_demand` e um `player_<skill>_rating`.

## 8. Estado

**Ronda 1 (2026-09-23)**: utilizador validou Q0.1, Q0.2, Q0.5, Q0.6 e as secções 1 (Aim), 2 (Speed),
4 (Tech) e 6 (Accuracy) sem alterações; refinou Q0.3 com a Q0.3b (Reading = visual (3) + padrões (4)
+ execução (6), não uma métrica única); excluiu a secção 5 (Flashlight).

**Ronda 2 (2026-09-23)**: fechada a Q0.4 — híbrido secção+agregado, com o agregado por mapa/skill
numa escala interpretável (ex.: "Aim 60/B+"), a ser usado diretamente como input da fase de
baselines (passo 7). Levantou a Q0.4b (que escala/normalização usar).

**Ronda 3 (2026-09-23)**: fechada a Q0.4b — pool de referência maior via **amostra de 5–10 mil
mapas** do dump (não o dump inteiro, por custo de tempo/disco; não só o repertório do jogador, por
o utilizador preferir uma escala mais próxima da comunidade).

**Ronda 4 (2026-09-23)**: fechada a estratégia de amostragem — **reservoir sampling** em streaming
sobre o `.tar.bz2`. Q0.4b fica totalmente resolvida ao nível de design.

**Ronda 5 (2026-09-23)**: fechada a fórmula de combinação da Q0.3b — `map_reading_demand =
sqrt((visual² + tech²) / 2)` (média quadrática/p-norm, p=2), depende da escala 0–100 da Q0.4b já
existir.

**Todos os pontos de design (Q0.1–Q0.6, Q0.3b, Q0.4b) estão fechados.**

**Implementado (2026-09-23)**: `src/osuml/beatmaps/reference_pool.py` +
`osuml maps reference-pool --path <dump> --n 8000 --seed 42 --version v1`. `reservoir_sample()`
faz Algorithm R em streaming sobre o dump, filtrando por modo (`_quick_mode`, regex leve no
`[General]`, sem parsear o ficheiro inteiro nem invocar o rosu-pp só para descartar candidatos) —
só osu!standard entra na amostra. `export_reference_pool()` corre `calc_difficulty` (nomod) sobre a
amostra e escreve `reference_pool_<version>.parquet` + manifest (com `seed` e `dump_sha256`, para
reprodutibilidade sem guardar os `.osu` amostrados). 4 testes em `tests/test_reference_pool.py`.

Corrido com o dump real: **8000/8000 mapas amostrados, 0 falhas de cálculo, ~5m8s** (dominado pela
leitura sequencial do `.tar.bz2`, igual à ordem de grandeza do `maps import` original). Distribuição
de `stars` plausível: mín. 0,96★, p10 2,02★, p25 2,52★, mediana 3,8★, p75 5,12★, p90 6,02★,
máx. 20,1★ — coerente com o perfil típico de mapas ranked (maioria em dificuldade baixa/média, cauda
longa de mapas muito difíceis). Todos os 8000 tinham `beatmap_id` legível do nome do ficheiro
(`<id>.osu`, convenção do dump oficial).

**Implementado (2026-09-23)**: `src/osuml/beatmaps/skills.py` +
`osuml maps skills --user "PXD Vieira" --version v0.2 --reference-version v1`. `ReferenceScale`
carrega a pool de referência e dá o percentil 0–100 de qualquer valor (`stars`, `aim`, `speed` por
agora) por posição na distribuição (`bisect`, sem dependência nova); `grade()` traduz o percentil
numa nota (S/A/B+/B/B-/C+/C/D+/D/F — convenção arbitrária deste projeto, documentada como tal).
`export_skill_scales()` lê `difficulty_<user_id>.parquet` + a pool e escreve `skills_<id>.parquet`
com `<skill>_score` e `<skill>_grade` por linha (mapa × combinação de mods). 3 testes em
`tests/test_skills.py`.

Corrido com dados reais: 228 linhas. Confirma o esperado — DT sobe sempre a nota face ao nomod do
mesmo mapa (ex.: mapa 140558 nomod Aim B-/Speed C → DT Aim B+/Speed B; mapa 746737 nomod Aim
B-/Speed C+ → DT Aim A/Speed B+). Distribuição de notas por `stars` no repertório do jogador:
maioria em B+/A (190 de 228), poucos em C/C+/B-/S — ou seja, face à pool de referência (8000 mapas
do dump), o jogador tende a jogar mapas acima da mediana geral.

**Implementado (2026-09-23) — os 4 eixos completos**: `src/osuml/beatmaps/hitfeatures.py`
(`density`, `reading_visual`, `tech_entropy`, todas nomod, fórmulas AR→preempt_ms e raio do circle
oficiais) reutilizado tanto em `beatmaps/export.py` (novas colunas em `beatmaps_<id>.parquet`) como
em `reference_pool.py` (pool recalculada como `v2`, mesma seed=42 → mesmos 8000 mapas,
reprodutibilidade confirmada pelo `dump_sha256` idêntico). `skills.py` combina Stamina (`density`) e
Reading (`sqrt((reading_visual²+tech_entropy²)/2)`, fórmula da Q0.3b) com Aim/Speed já existentes.
Stamina simplificada por decisão do utilizador: densidade de objetos (objetos/segundo) em vez de
`Strains` do rosu-pp — mapas curtos e densos treinam stamina, mapas longos e esparsos treinam
consistência, não stamina.

Corrido com dados reais: perfil do jogador (mediana da nota 0-100 dos mapas jogados, ver Q0.4b) —
Aim 80,8 (A), Speed 71,9 (B+), Stamina 79,0 (B+/A), **Reading 47,3 (perto da mediana geral,
único eixo onde o jogador não procura mapas acima da média)**. Desde o regresso (meados de
set/2026), a mediana de Reading sobe muito (42,0 → 62,1), mais do que os outros eixos — ver
"primeira leitura" na conversa para a análise completa (correlações com accuracy/pp/fails,
correlações cruzadas entre eixos — Speed e Reading correlacionam a 0,59, não são totalmente
independentes nesta primeira versão).

Próximo passo: passo 5 do `CLAUDE.md` (features de hit objects mais finas: distância, ângulo,
sliders) e, mais tarde, formalizar a "leitura do jogador" como relatório reutilizável se for útil.
