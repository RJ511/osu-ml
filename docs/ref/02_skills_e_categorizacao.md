# Skills, escalas e categorização

Nomenclatura das skills, os 4 eixos, escala aberta, Reading (port do lazer), categorização de mapas/jogadores, visualizador e painel Explorar.

> Secção movida do `CLAUDE.md` a 2026-10-15 (reorganização). O conteúdo é o original, por ordem cronológica de escrita; as decisões que não se revertem sem perguntar continuam resumidas no `CLAUDE.md`. Se algo aqui contradiz o `CLAUDE.md`, vale o `CLAUDE.md`.

### Nomenclatura de "skills" à la comunidade: documento de discussão em `docs/skills_comunidade.md`
O pedido do utilizador é que, quando o sistema vier a representar as capacidades do jogador (ou a
dificuldade de um mapa) por "skills", os nomes sejam os da comunidade — ex. **speed, aim, stream,
stamina, reading, tech, jump, flow aim, sharp aim, finger control, precision, alt** — em vez de
nomes técnicos arbitrários de um clustering.

Achado-chave: **Aim, Speed e Flashlight são as únicas "skills" formais/oficiais para
osu!standard** (as que o jogo e o `rosu-pp` calculam). Os restantes termos são vocabulário informal
sem fórmula própria — descrevem padrões de hit objects. `docs/skills_comunidade.md` desenvolve isto
numa lista extensiva de perguntas (uma por decisão em aberto: thresholds, mapa-vs-jogador,
mapa-vs-secção, o que dá para calcular sem replay, etc.), cada uma já com uma proposta concreta de
interpretação/fórmula a partir das colunas que já temos em `hitobjects_<id>.parquet` e
`difficulty_<id>.parquet` — para validar com o utilizador antes de implementar. **Nada disto está
implementado ainda** (ver "Não fazer ainda" abaixo); fica para quando o passo 5/6 do roadmap for
retomado.

**Discussão fechada até à ronda 3 (2026-09-23)**: eixos largos aceites = Aim, Speed, Stamina,
Reading (Reading = visual/AR + padrões/tech + execução/clique, não uma métrica única — ver Q0.3b no
documento); todas as skills passam a ter par `map_<skill>_demand` + `player_<skill>_rating`;
Flashlight excluído do sistema por decisão do utilizador. Granularidade (Q0.4): híbrido
secção+agregado, sempre com um valor agregado por mapa/skill numa escala interpretável (ex.:
"Aim 60/B+"), a usar como input direto da fase de baselines. Escala/normalização (Q0.4b, **fechada**):
pool de referência via **amostra de 5–10 mil mapas** do dump, extraída por **reservoir sampling**
em streaming sobre o `.tar.bz2` (não o dump inteiro — ~20-30 min e 3-6 GB de disco, orçamentado mas
rejeitado por âmbito; não estratificado por dificuldade, por exigir pedidos extra à API; não só os
216 mapas do jogador, para ficar mais perto de uma escala de comunidade). Fórmula de combinação de
Reading (Q0.3b, **fechada**): `map_reading_demand = sqrt((visual² + tech²) / 2)` (média quadrática,
mesma filosofia do jogo ao combinar aim/speed), depende da escala 0–100 da Q0.4b já existir. **Todos
os pontos de design estão fechados.**

**Pool de referência implementada e corrida**: `src/osuml/beatmaps/reference_pool.py` +
`osuml maps reference-pool --path <dump> --n 8000 --seed 42 --version v1` (reservoir sampling,
filtro de modo por regex leve, sem persistir os `.osu` — reprodutível por `dump_sha256`+`seed`).
Corrido com o dump real: **8000/8000 mapas osu!standard, 0 falhas, ~5m8s**,
`reference_pool_v1.parquet` em `data/processed/v1/`, distribuição de `stars` plausível (mediana
3,8★, min 0,96★, máx 20,1★). Testes em `tests/test_reference_pool.py`.

**Escalas 0-100 implementadas e corridas**: `src/osuml/beatmaps/skills.py` +
`osuml maps skills --user ... --version v0.2 --reference-version v1` → `skills_13745526.parquet`
(228 linhas, `<skill>_score` 0–100 + `<skill>_grade` S/A/B+/.../F por mapa×mods, para `stars`,
`aim`, `speed`).

### Os 4 eixos completos (Aim, Speed, Stamina, Reading): FEITO
`src/osuml/beatmaps/hitfeatures.py`: `density` (Stamina, simplificada por decisão do utilizador —
objetos/segundo, não `Strains` do rosu-pp), `reading_visual` (AR→preempt_ms oficial + raio do
circle oficial, overlaps espaciais) e `tech_entropy` (entropia normalizada da distribuição de
snaps rítmicos). Reutilizado em `beatmaps/export.py` (novas colunas em `beatmaps_<id>.parquet`) e
em `reference_pool.py` (pool recalculada como **v2**, mesma seed=42 → mesmos 8000 mapas,
`dump_sha256` idêntico ao v1, confirma a reprodutibilidade). `skills.py` agora cobre os 4 eixos:
Reading = `sqrt((reading_visual_score² + tech_score²) / 2)` (Q0.3b), Stamina = `density` escalado.
Testes em `tests/test_hitfeatures.py` e `tests/test_skills.py` atualizados (42 no total).

**Primeira leitura do jogador (2026-09-23)**: perfil por mediana da nota 0-100 dos mapas jogados —
Aim 80,8 (A), Speed 71,9 (B+), Stamina 79,0 (B+/A), **Reading 47,3 (~mediana geral — único eixo
onde não procura mapas acima da média)**. Desde o regresso (set/2026) a mediana de Reading sobe
muito mais (42,0→62,1) do que os outros eixos. Correlações com accuracy todas fracas e negativas
(-0,11 a -0,25, esperado — mais dificuldade, menos accuracy); com pp, Aim é a única positiva
(0,158), Reading é negativa (-0,184). Taxa de fail mais alta e concentrada na faixa "B" de Reading
(21/50 ≈ 42%). Eixos não são totalmente independentes: Speed↔Reading correlacionam a 0,59 nesta
primeira versão (esperado dado que Tech deriva de padrões rítmicos também presentes em Speed).
**Caveats**: N pequeno (241 scores, 46 recentes), retries do mesmo mapa não são independentes
(tratados como score-a-score nesta análise descritiva), Reading só nomod. Análise feita ad-hoc,
ainda não persistida como script/comando do projeto.

### Visualizador de skills (artefacto + cópia local, ambos interativos)
Publicado em claude.ai (privado, `db` para QA partilhado na nuvem) e também guardado em
`reports/skills_viewer.html` + `reports/skills_viewer_data.json` (fora do git, tal como `data/`).
**Bug corrigido**: a primeira versão local crashava ao abrir como ficheiro (`claude.use(...)`
referenciava `claude` como global, que não existe fora do artefacto do claude.ai — dá
`ReferenceError`, não só `null`). Corrigido com `typeof claude === "undefined"` antes de chamar, e
com fallback para `localStorage` quando não há `db` — por isso o ficheiro local **também é
interativo** (QA fica guardado só naquele navegador; o artefacto do claude.ai guarda na nuvem,
partilhado). Regenerar continua manual por agora (script ad-hoc).

### Escala de notas expandida (decisão do utilizador, 2026-09-23)
Chegar a "S" era fácil demais (percentil 90 já batia em mapas de ~6★) para servir de recompensa
psicológica. `_GRADE_BINS` em `skills.py` ganhou 4 níveis acima de S, sem mexer em nada abaixo:
`X (≥99.9%) > SSS (≥99%) > SS (≥97%) > S+ (≥94%) > S (≥90%) > A (≥80%) > ...` (resto inalterado).
Com a pool de 8000 mapas, X fica reservado a ~8 mapas (0,1%, ~10★+) — quase inatingível, como
pedido ("X são praticamente os atributos dos top jogadores mundiais"). Calibrar X/SSS contra
atributos REAIS de top players (não só mapas extremos) é exatamente o que a expansão multi-jogador
(abaixo) haveria de permitir.

### Achados da verificação de sanidade da escala 0-100 (2026-09-23)
- Confirmado: a soma `aim+speed` médio sobe consistentemente por faixa de estrelas na pool de
  referência (≤3★: 2.11 → 3-6★: 4.30 → 6-9★: 6.41 → >9★: 9.52). A relação estrelas↔skills comporta-se
  como esperado.
- **Achado importante**: só 0,10% da pool (8 mapas) tem ≥10★, só 0,03% (2 mapas) tem ≥11★ (máx.
  20,1★ na pool). O valor de `aim` bruto que já corresponde à nota **S** (percentil 90) é **3,19** —
  e mapas do jogador de ~6★ já têm `aim` bruto por volta de 2,4–3,6, por isso já caem em S/A. Isto
  **não é um bug**: mapas de aim muito puro são raros na população geral de mapas ranked,
  independentemente da sua nota de estrelas — um percentil reflete exatamente essa raridade. Mas é
  uma limitação a ter em conta: a escala 0-100 mede "mais difícil que X% dos mapas ranked", não
  "quão perto está do teto teórico do jogo" — são coisas diferentes, e se algum dia se quiser a
  segunda leitura, precisa de uma normalização diferente (min-max contra o máximo observado, não
  percentil).
- **Teto empírico confirmado**: o mapa de maior ★ onde o jogador já teve accuracy ≥88% é
  **Stella-rium [Celestial], 6,16★, 94,46% (nomod, PASS)**. Isto é uma leitura direta dos dados
  (não uma previsão para mapas nunca jogados).

### Categorização de mapas e jogadores `osuml categorize` + painel unificado (2026-09-23)
`osuml panel --port 8765` mostra, no mesmo painel (iframes), as recolhas à API e a categorização em
tempo real (`categorize/`: `core.py`, `server.py`, `export.py`); `osuml categorize --port 8766` corre só
a categorização. **0 pedidos à API.** Cada par (beatmap_id, mods sem `CL`) é categorizado **uma vez** e
guardado em `map_categories` (mapas repetidos, entre jogadores ou execuções, não são recalculados);
sem `.osu` → `no_file` (198 dos 7 611 mapas dos 75 jogadores ficaram sem ficheiro: 7 413 importados do
dump; obtê-los = `maps fetch` = pedidos web → **perguntar primeiro**). Rating do jogador por eixo = P90
do eixo nas plays passadas com acc ≥ 90% (`confident` se ≥ 10), Spearman rating↔pp como validação.
Cada categoria guarda o `scheme` (`SCHEME` em `categorize/core.py`). **Regra: mudar a definição de uma
skill ou da escala ⇒ subir o `SCHEME` e re-analisar tudo** (pedido do utilizador: mapas e jogadores
analisados antes da alteração das skills têm de ser reanalisados). Os `_saved` de outro esquema são
ignorados e sobrescritos (mesma PK), pelo que basta voltar a correr `categorize`.

### Reading reformulado + escala aberta (2026-09-23, `cat_v2`) — decisão do utilizador
- **Diagnóstico**: o Reading antigo (`reading_visual`+`tech_entropy` em percentis, `hitfeatures.py`) era
  quase só densidade e estava invertido/desligado das outras skills (mapa HD com Aim ≈ 100 e Speed 99,6
  saía com Reading 12). Reading é uma skill complementar, ligada às restantes: quem não lê o mapa não
  o joga. **Tech deixou de fazer parte do Reading** (`tech_entropy`/`reading_visual` ficam só como
  features descritivas).
- **Novo Reading** = port em Python (`src/osuml/beatmaps/reading.py`) da skill oficial do osu!lazer
  (`Reading.cs`, `ReadingEvaluator.cs`, `HarmonicSkill.cs`, `OsuDifficultyHitObject.cs`; lido do branch
  master em 2026-09-23): preempt (AR), densidade visível, HD, repetição de ângulos, velocidade, bónus de
  BPM, tira 0,8^(ms/1000), primeiros 60 s reduzidos, soma harmónica; `rating = sqrt(valor)·0,0675`.
  **Com os mods** (DT/HT clock rate, HR/EZ, HD), integrado em `calc_difficulty` (`difficulty.py`) ⇒ o
  Reading vem em `difficulty_*.parquet`, na pool e no categorizador. `rosu-pp-py` devolve `reading=None`
  em std, por isso não há valor oficial para comparar. **Aproximações** (docstring do módulo): sem
  stacking, caminho de sliders aproximado (Bézier/arco amostrados), sem speed_change/Magnetised.
  Propriedades verificadas em `tests/test_reading.py`: sobe com DT/HD/velocidade, monótono em AR em mapas
  esparsos, repetição de ângulos facilita. Nota: **não é monótono em AR** (AR baixo com muitos objetos
  visíveis também pesa), igual ao original.
- **Escala aberta** (`beatmaps/skills.py`): `score = 50 + 20·z`, `z` = (log(valor) − μ)/σ ajustado à pool
  nomod (log-normal), **sem teto nem chão** (nenhuma skill chega a 100: 50 = mapa mediano da pool). Nota
  por z (X ≥ 111,8; SSS ≥ 96,6; SS ≥ 87,6; S+ ≥ 81; S ≥ 75,6; A ≥ 66,8; B+ ≥ 60,5; B ≥ 55; B- ≥ 50; C+ ≥ 45;
  C ≥ 39,5; D+ ≥ 33,2; D ≥ 24,4; senão F — equivalem aos antigos cortes de percentil). O percentil
  ("top x %") fica como `*_pct`, secundário. Pool nova `reference_pool_v3` (mesma semente 42, 8000 mapas;
  `osuml maps reference-pool --path <dump> --n 8000 --seed 42 --version v3`, 0 pedidos); esquema
  `ref_v3/cat_v2`. O `data/processed/v0.2/skills_13745526.parquet` e `reports/skills_viewer.html` (PXD)
  estão **desatualizados** (Reading antigo) até serem regenerados com `maps skills --reference-version v3`.
- Bugs corrigidos nesta fase: `cmd_poll` apagado por engano ao editar `cli.py` (o painel morria ao arrancar;
  agora `_handlers()` + teste) e ações que devolviam `None` respondiam 404 (sentinela `_NOT_FOUND`).

### Painel "Explorar" (2026-09-24) — pesquisar jogadores e mapas
`osuml panel --port 8765` tem agora 3 separadores: **Recolha**, **Categorização** (em direto) e **Explorar**
(mais "Lado a lado" = Recolha + Categorização). `src/osuml/explore/` (`core.py` = `Explorer`, consultas só de
leitura; `server.py` = página + ações POST com token; 6 testes em `tests/test_explore.py`; 0 pedidos à API):
- **Jogadores**: pesquisa por nome ou id; detalhe com rating P90 e "típico" por eixo (Aim/Speed/Stamina/
  Reading/★), e todas as plays (data, mapa, mods, ★, acc, pp, pass/fail, nota por eixo), com filtro
  (texto, passadas/fails) e ordenação por coluna. Clicar num mapa abre o mapa.
- **Mapas**: pesquisa por artista/título/dificuldade/mapper (várias palavras) ou id; detalhe com atributos
  por combinação de mods (nota+letra+"top x %" no tooltip, valores reais aim/speed/reading/obj/s, AR/CS/OD/HP,
  objetos) e as plays de todos os jogadores nesse mapa. Clicar num jogador abre o jogador.
- Links diretos: `http://127.0.0.1:8765/explore#p/<user_id>` e `#m/<beatmap_id>`.
- **Verificação por jogador** (2026-09-24, `explore/check.py` + `Tracker.check_now`): no detalhe do jogador
  aparecem a **última verificação** (último pedido 200 a `/users/{id}/scores/*` em `api_requests`: recolhas,
  `poll` agendado e verificações manuais), a próxima agendada e a **última categorização** (`player_profiles.
  computed_at`); a lista mostra "verificado há X". Botão **"Verificar agora (1 pedido à API)"**: 1 pedido `recent`
  (+`best` só se o jogador nunca o teve), com as proteções do `poll` (PAUSE, teto de 400/24 h, `ApiLock` com
  pausa de 1,1 s, jogadores inativos recusados), depois recalcula o perfil desse jogador (`CategorizeController.
  recategorize_player`, mapas já categorizados vêm da cache) e reagenda o `poll` (14–22 h). **Intervalo de 5 s**:
  o botão fica desativado com contagem decrescente e o servidor recusa outra verificação do mesmo jogador antes
  de 5 s (também depois de uma tentativa falhada). É a única ação do painel Explorar que faz pedidos à API, e só
  quando se carrega no botão. Mapas sem `.osu` **não são necessários** (decisão do utilizador): não se fazem
  `maps fetch`; aparecem só como "—".
- Nota: `mods_effective` não normaliza NC→DT, por isso `DT,HD` e `HD,NC` aparecem como variantes separadas
  (mesmos valores). Recategorização `cat_v2` concluída em 2026-09-24: 75 jogadores, 11 173 pares novos, 0 erros,
  239 visitas `no_file`; Spearman rating↔pp: Aim 0,93, Speed 0,96, Stamina 0,86, Reading 0,90, ★ 0,95.
