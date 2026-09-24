# CLAUDE.md — osu! ML Skill & Recommendation System

Contexto para o Claude Code. Lê este ficheiro e `docs/design.md` antes de mexer no código.
Responde em **português de Portugal**, de forma clara, direta e imparcial.

## Objetivo

Sistema **ML-first** que aprende uma representação das capacidades de um jogador de osu! a partir
do histórico de scores e da estrutura dos beatmaps, para mais tarde prever performance e recomendar
mapas. A visão completa está em `docs/design.md` (documento original do projeto).

Regra do roadmap: **não saltar para a fase seguinte sem a anterior produzir dados verificáveis e
reproduzíveis.** Privilegiar correção, qualidade de dados, reprodutibilidade, cumprimento das regras
da API e utilidade futura para ML, acima da rapidez.

## Decisões tomadas (não reverter sem perguntar)

- Jogador da Fase 0: **"PXD Vieira"** (user_id **13745526**, modo osu!), que é o próprio utilizador.
- Dados de scores **apenas pela osu!API v2 oficial**. **Não usar o osu! score cache (oSC)** nem
  outros serviços de terceiros (osustats, osu!daily, mirrors) sem perguntar.
- Autenticação: OAuth2 **Client Credentials** (scope `public`). Token só em memória; nunca em logs,
  disco ou git. Os logs do `httpx`/`httpcore` estão silenciados de propósito (registariam o header
  Authorization).
- **Python 3.14** (requisito do pacote: `>=3.11`).
- Storage: SQLite por omissão (`data/osuml.db`), compatível com PostgreSQL via `OSUML_DATABASE_URL`.
  Datasets ML em Parquet.
- Ficheiros `.osu`: fonte principal é o **dump oficial do data.ppy.sh** (ranked/loved). Fallback
  opcional `osu.ppy.sh/osu/{id}` (rota do site, **não** documentada na API v2), só para os poucos
  mapas fora do dump.
- Espelho opcional em **AWS S3**: bucket `osu-ml-skill`, região `eu-west-1` (`OSUML_S3_BUCKET`/
  `OSUML_S3_REGION` no `.env`, valores por omissão já são estes). Cobre `data/raw/`,
  `data/processed/` e o dump `.tar.bz2`. **Só corre quando pedido explicitamente** (`osuml sync-s3`)
  — nunca automático a seguir a um collect/export/import. Credenciais AWS nunca geridas pelo nosso
  código: o `boto3` lê-as sozinho do ambiente (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/
  `AWS_PROFILE`).

## Regras da API (obrigatórias)

- ≤ 60 pedidos/min: intervalo mínimo de 1,1 s entre pedidos, collector sequencial, sem concorrência.
- Retry com exponential backoff + jitter em 429/5xx/rede; respeitar `Retry-After`.
- Nunca repetir pedidos de dados já guardados (TTL de utilizador 24h, snapshot 7 dias).
- Polling irregular (cron com `--max-start-delay`), sem harvesting.
- Nunca assumir endpoints/campos: verificar a documentação atual (https://osu.ppy.sh/docs/) ou o
  código do osu-web.

Factos verificados no código do osu-web:
- `GET /users/{id}/scores/{best|firsts|pinned|recent}`; `limit` 1–100 (omissão 5).
- `recent`: só `ended_at` nas **últimas 24h** e **offset+limit ≤ 100**; `include_fails=1` inclui fails.
- `best/firsts/pinned`: paginação até a página vir incompleta.
- Lookup por username: `/users/@{username}/{mode}`.
- Header `x-api-version: 20240529` (objeto Score novo, ids unificados stable/lazer).

## Estado atual

### Fase 0 — collector de scores: FEITA e validada com dados reais
- `python -m osuml collect --user "PXD Vieira"` corre com sucesso.
- Primeiro export (v0.1): **241 scores únicos, 216 mapas** (todos com metadata), **34 fails**
  (14%), intervalo 2021-10-26 → 2026-09-22, 0 lacunas. Fontes: best 200, recent 44 (3 em comum).
  `firsts` e `pinned` vazios (normal para este jogador).
- Agendamento recomendado: 3–4 execuções/dia (o `recent` só cobre 24h e 100 scores).

### Export com colunas derivadas: FEITO e corrido como v0.2
`src/osuml/dataset/derived.py`: `is_legacy`, `mods_effective` (sem `CL`), `progress`,
`session_id` (pausa > 30 min = sessão nova), `attempt_index`, e `beatmap_status` no export.
`scores_13745526.parquet` v0.2: 241 linhas, colunas derivadas confirmadas (108 legacy / 133 lazer,
`attempt_index` máx. 8, 109 sessões).

### Fase de beatmaps: import do dump + fetch + export FEITO e validado (216/216 mapas)
- `osuml maps import --path <dump|pasta|zip>`: lê em streaming e guarda só os mapas pedidos em
  `data/raw/osu_files/{md5}.osu`, identificados pelo MD5 = `beatmaps.checksum` da API.
- `osuml maps status` (mostra `missing_by_status`), `osuml maps fetch` (fallback),
  `osuml maps export --version v0.2` → `beatmaps_<id>.parquet` + `hitobjects_<id>.parquet`.
- Parser em `src/osuml/beatmaps/parser.py` preserva hit objects crus (tempo, x, y, tipo, curva,
  slides, length, end_time calculado com beatLength+SV, beatLength e SV ativos).
- Corrido `maps import --path 2026_09_01_osu_files.tar.bz2 --user "PXD Vieira" --dump`:
  215/216 mapas importados por MD5 (0 imports por nome/checksum a divergir). Faltava só o
  beatmap_id **4529109** (`graveyard` — fora do dump ranked/loved, esperado). O utilizador optou
  por usar `maps fetch` (1 pedido a `osu.ppy.sh/osu/4529109`, 200 OK, checksum bate certo) →
  `maps status` fica **216/216, missing 0, checksum_mismatch []**.
- Bug de teste corrigido (não afeta produção): `tests/test_beatmaps.py` escrevia o `.osu` de
  fixture com `Path.write_text(...)`, que no Windows traduz `\n`→`\r\n` e muda o MD5 face ao
  checksum esperado, fazendo o import por pasta falhar sempre no teste. Passou a `write_bytes`.
- `2026_09_01_osu_files.tar.bz2` (dump, ~1.4 GB) fica na raiz do repo, fora do git
  (`*.tar.bz2` acrescentado ao `.gitignore`).

### Fase de dificuldade local (passo 4): FEITA e validada com dados reais
- **Python 3.14 confirmado**: `rosu-pp-py` 4.0.2 tem wheel pré-compilado
  `cp314-cp314-win_amd64` no PyPI — instala com `pip install rosu-pp-py`, sem precisar de Rust/
  maturin nem compilar nada. Módulo Python chama-se `rosu_pp_py` (não `rosupp`). Adicionado como
  extra opcional `difficulty` em `pyproject.toml` (`pip install .[difficulty]`).
- Novo módulo `src/osuml/beatmaps/difficulty.py` + comando `osuml maps difficulty --user ... --version v0.2`
  (0 pedidos — só lê `.osu` já importados). `mods_by_beatmap()` calcula, por mapa, o conjunto de
  combinações de mods (`mods_effective`, sem `CL`) que o jogador realmente jogou, mais nomod
  sempre incluído. `export_difficulty()` corre `rosu_pp_py.Difficulty(mods=...).calculate(...)`
  para cada par (mapa, combinação) e escreve `difficulty_<user_id>.parquet` +
  `manifest_difficulty_<user_id>.json`.
- Corrido com dados reais: **216 mapas, 228 linhas** (216 nomod + 12 combinações extra realmente
  jogadas: DT ×7, DT+HD ×2, HD ×1, SD ×1, AC ×1), 0 mapas em falta, < 2 s. Confirmado com DT que
  `aim`/`speed`/`stars` sobem corretamente (ex.: mapa 140558 nomod 3.61★ → DT 5.02★), validando a
  observação de que `nomod_star_rating` da API subestima scores com DT.
- Fluxo: `rosu_pp_py.Beatmap(content=...)` + `rosu_pp_py.Difficulty(mods=lista_de_acronimos).calculate(m)`
  → `DifficultyAttributes`. **Importante**: os mods têm de ir como **lista de acrónimos**
  (`["DT","HD"]`) ou string concatenada (`"DTHD"`) — a string com vírgulas do `mods_effective`
  (`"DT,HD"`) **não funciona bem** (a vírgula corta o parsing e só o primeiro mod conta); por isso
  `difficulty.py` faz sempre `mods.split(",")` antes de chamar o rosu-pp.
- **Risco de dados silencioso detetado e documentado**: se um acrónimo de mod não for reconhecido
  pelo rosu-pp, a biblioteca **ignora-o silenciosamente** (sem erro nem aviso) e calcula como se
  esse mod não existisse — testado com um acrónimo inventado (`"ZZ"`) e deu o mesmo resultado do
  nomod. Nos dados reais os únicos mods jogados são DT, HD, SD, AC (todos reconhecidos; SD/AC não
  alteram o SR por desenho do próprio jogo, o que é o comportamento esperado) — não há sinal de
  problema agora, mas fica registado como limitação a vigiar se aparecerem mods mais recentes/raros.
- **Para osu!standard, `DifficultyAttributes` só expõe três "skills" formais**: `aim`, `speed` e
  `flashlight` (0.0 sem o mod FL), mais métricas derivadas (`slider_factor`,
  `aim_difficult_strain_count`, `speed_difficult_strain_count`, `speed_note_count`,
  `aim/speed_top_weighted_slider_factor`). Também há `Strains` (e `GradualDifficulty`), que dão a
  série temporal de `aim`, `aim_no_sliders`, `speed` e `flashlight` por secção de tempo — útil
  para localizar *onde* no mapa a dificuldade sobe (ex.: identificar um trecho de stream), mas
  continua a ser só aim/speed/flashlight, não uma categoria "stream" nativa.
- Campos como `stamina`, `reading`, `rhythm`, `color`, `mono_stamina_factor`,
  `mechanical_difficulty`, `consistency_factor` existem na API mas **só para osu!taiko**
  (confirmado no `.pyi` da biblioteca — vêm a `None` para osu!standard). Não há, na biblioteca
  nem no algoritmo de pp oficial do osu!standard, uma decomposição pronta em termos como
  "stream", "jump", "stamina" ou "reading".

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

### Espelho S3: FEITO
`src/osuml/storage/s3.py` + `osuml sync-s3 [--dry-run] [--dump-path ...]`. Espelha `data/raw/`,
`data/processed/` e o dump `.tar.bz2` para `s3://osu-ml-skill` (`eu-west-1`). Idempotente por MD5
via `ETag` (ficheiros grandes com upload multipart, incl. o dump, não têm deteção fiável de "já
enviado" e são sempre reenviados — limitação documentada, não bloqueante). `--dry-run` nunca
contacta o S3 (só precisa do `boto3` instalado, não de credenciais). Testado com um cliente S3 falso
(sem rede) em `tests/test_s3.py`; testado manualmente em `--dry-run` real (221 ficheiros em
`data/raw/`, 13 em `data/processed/`, 1 dump — sem credenciais AWS configuradas nesta máquina, por
isso só `--dry-run` foi validado, nunca um upload real). `boto3` como extra opcional `s3` em
`pyproject.toml`.

## Observações sobre os dados reais (importantes para ML)

1. **Dois regimes de scoring**: 108 scores stable (`legacy_score_id` preenchido, mod `CL`,
   `started_at` NULL, statistics só great/ok/meh/miss) e 133 lazer (statistics com slider tails,
   ticks, bónus). Accuracy não diretamente comparável entre regimes → usar `is_legacy`; `CL` não é
   feature de mods.
2. Pass sem pp (score 7544699754, mapa 3118127) é um mapa **loved** — correto, não é bug.
3. Fails: accuracy é parcial; o sinal útil é `progress` (mediana 7%, máx. 46%).
4. Retries do mesmo mapa (até 8 seguidos) **não são independentes** → split treino/val/teste
   **por sessão**, nunca aleatório por score.
5. O utilizador **voltou a jogar em meados de setembro de 2026 após uma pausa**; está bem abaixo do
   seu `best`. Tratar o best como pico passado/prior, e as sessões desde o regresso como estado atual.
6. `nomod_star_rating` subestima scores com DT (9 scores) → SR com mods virá do cálculo local.

## Próximos passos (por ordem)

1. [x] Descarregar o dump de `.osu` do data.ppy.sh e correr
       `python -m osuml maps import --path <arquivo> --user "PXD Vieira"`; ver `maps status`.
2. [x] Mapa em falta fora do dump (só 1: `4529109`, graveyard): decidido com o utilizador usar
       `maps fetch` → 216/216 mapas com ficheiro.
3. [x] `python -m osuml export ... --version v0.2` e `python -m osuml maps export ... --version v0.2`.
       Verificado: `beatmaps_13745526.parquet` (216 linhas, `checksum_match` True em todas),
       `hitobjects_13745526.parquet` (93 539 linhas: 57 610 circles, 35 744 sliders, 185 spinners;
       216 beatmap_id distintos, `maps_with_parse_warnings` vazio), `scores_13745526.parquet`
       (241 linhas, colunas derivadas presentes, `attempt_index` máx. 8, 109 sessões distintas).
4. [x] Atributos de dificuldade por mapa **e por combinação de mods**, calculados localmente com
       `rosu-pp-py` (Python 3.14 confirmado). `osuml maps difficulty --user ... --version v0.2`
       → `difficulty_13745526.parquet` (228 linhas, 216 mapas, 0 em falta). Ver secção "Fase de
       dificuldade local" acima; ponto em aberto sobre nomenclatura de skills à la comunidade
       continua por decidir (não é resolvido por esta fase, só a base de cálculo).
5. [ ] Features de hit objects: distância, ângulo, delta time, velocidade, densidade, streams/bursts,
       sliders (camada 2 do design). Hipóteses, não verdade.
6. [ ] **Análise descritiva dos fails**: `progress × n_objetos` → objeto/timestamp do fail; comparar
       features da janela anterior (5–10 s) com o resto do mapa. Sem ML ainda.
7. [ ] Só depois: baselines (SR, SR+AR/OD/CS/BPM, LightGBM) com split temporal por sessão.

Não fazer ainda: treinar modelos ou clustering de skills com ~40 tentativas recentes.

### Recolha agendada: configurada (ainda não correu — verificado 2026-09-23 16:56)
Tarefa do Windows Task Scheduler `osuml-collect` (12/12h, `--max-start-delay 1800`, corre
`osuml collect --user "PXD Vieira"` no `.venv` do projeto). `LastTaskResult 267011` = nunca correu: o
primeiro disparo (criado com `-At (Get-Date)`) passou sem executar; **próxima execução 03:42 de
24/09**, depois de 12 em 12h. Corrigido: já não é bloqueada por bateria; limite de execução 90 min
(pode esperar até 30 min pelo bloqueio da API + jitter de 30 min). Confirmar amanhã em
`Get-ScheduledTaskInfo osuml-collect` e em `api_requests`. Ponto 4 do utilizador (RunPod) fica em
espera até haver treino real que precise de GPU — não é necessário para LightGBM.

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

### Direção confirmada: modelo vai prever a partir do perfil do jogador (não só PXD Vieira)
O utilizador confirmou: o objetivo passa a ser prever desempenho **a partir do perfil de um
jogador qualquer** (não só ele), o que implica recolher também top players e/ou jogadores
aleatórios regulares para dar volume/variedade — isto **muda a decisão de âmbito da Fase 0** ("só
PXD Vieira"). Direção aceite; a escolha de **quantos e quais jogadores** ainda está por decidir
com o utilizador antes de qualquer pedido novo à API (ver regra abaixo). Contexto já discutido:
cada jogador novo dá centenas de scores de imediato via `best` (não é preciso esperar dias);
top players e aleatórios dão distribuições de skill muito diferentes (mistura dos dois é
provavelmente melhor); dados públicos, sem problema de ToS/privacidade da API v2.

### Fonte adicional em avaliação: dumps oficiais de performance do data.ppy.sh (nada descarregado ainda)
Verificado a 2026-09-23 em https://data.ppy.sh/ (mesmo host do dump de `.osu`): existem
`2026_09_01_performance_osu_{top_1000, top_10000, random_10000}.tar.bz2` (1,30 GB / 5,49 GB /
1,08 GB comprimidos, via `HEAD`) e `osu_user_beatmap_playcount.sql` (154 MB), mais os equivalentes
para taiko/catch/mania. O README do `ppy/osu-performance` diz que contêm "top 10 000 utilizadores +
amostra aleatória de 10 000 + tabelas auxiliares", mas **não lista as tabelas/colunas** — o esquema
real ainda por confirmar (só vendo o conteúdo). **Licença** (`LICENCE.txt`): dados "para análise
estatística e teste de subsistemas do osu!"; **uso em produção/exposição pública NÃO é permitido sem
autorização de ppy** (contact@ppy.sh) → manter tudo privado (S3 privado, artefactos privados) e pedir
autorização antes de qualquer serviço público/produção (relevante para o ponto RunPod).
Isto **muda a decisão "scores só pela API v2"** → só avançar com confirmação explícita do utilizador.

**Descarregados com autorização do utilizador (2026-09-23)** para `data/external/performance/`
(fora do git; NÃO espelhados pelo `sync-s3`, que só cobre `data/raw`+`data/processed`):
`..._osu_random_10000.tar.bz2` (1,08 GB) e `..._osu_top_1000.tar.bz2` (1,30 GB). **Não**
descarregados: `top_10000` (5,49 GB) nem o `osu_user_beatmap_playcount.sql` avulso (o pacote
random já traz a sua própria versão, 141 MB). Esquema verificado no `random_10000` (só o início de
cada tabela, contagens de linhas ainda por medir) — membros do tar, tamanho descomprimido:
- `scores.sql` (1,34 GB): **mesmo formato do score da API v2** (`data` JSON com `mods`/`statistics`/
  `maximum_statistics`, `passed`, `accuracy`, `pp`, `legacy_score_id`, `started_at`, `ended_at`,
  `ruleset_id`) — inclui histórico legacy convertido (`CL`) e, por `passed`, potencialmente fails.
- `osu_scores_high.sql` (432 MB): melhor score por jogador/mapa, formato legacy (count300/100/50/
  miss, `enabled_mods` como bitmask, `pp`, `date`).
- `osu_user_stats.sql` (1,9 MB): por jogador `rank_score` (pp), `rank_score_index`, `rank`,
  `playcount`, `last_played`, `total_seconds_played`, `fail_count`, `exit_count` → permite escolher
  por banda de rank **e filtrar por atividade recente** sem API. (Qual das colunas de rank é o rank
  global real — `rank` vs `rank_score_index` — por confirmar: nos primeiros registos divergem.)
- `sample_users.sql`: `user_id` → `username` (dados pessoais de terceiros: usar só o `user_id` nos
  datasets derivados, manter privado).
- `osu_beatmaps.sql` (todos os mapas: `difficultyrating`, `bpm`, `playcount`, `passcount`, ...),
  `osu_beatmap_difficulty_attribs.sql` (4,7 GB — atributos oficiais por combinação de mods: dá para
  **validar o nosso rosu-pp contra a referência oficial**), `osu_beatmap_failtimes.sql` (histograma
  de 100 baldes de **onde falham/saem os jogadores em cada mapa** — relevante para o passo 6),
  `osu_user_beatmap_playcount.sql` (playcount por jogador/mapa).
Ainda **nada foi importado nem transformado**; não há MySQL instalado — plano: parsing em streaming
(como no dump de `.osu`), não `cat *.sql | mysql`.

### Painel multi-jogador v1 (2026-09-23): selecionado a partir dos dumps, sem API
`src/osuml/external/sqldump.py` (leitor mysqldump em streaming, 4 testes) + `data/processed/players/`
(`panel_candidates.json` → `panel_activity.json` → `panel_final.json` → **`panel_v1.json`**, só
`user_id`, sem usernames). Confirmado: `rank_score_index` é o rank global (top_1000 → 1..1000);
`rank` é legado. Critério: `last_played` ≤7 dias antes do snapshot (2026-09-01) e playcount ≥1000;
sobreamostragem 5× (seed 42) e depois **≥12 dias ativos nas 4 semanas anteriores** (≈3 dias/semana),
medido em `scores.sql` (3,95 M linhas lidas, 257 s, só ruleset osu; datas extraídas por regex — é uma
aproximação). Resultado: top20 (sem filtro) 20/20; 1000-10000 5/5; 10000-25000 5/5; 25000-50000 5/5;
50000-100000 10/10; 100000-200000 10/10; 200000-500000 10/10; **500000+ só 5/10** (só 5 dos 50
candidatos passaram o filtro de atividade). Total 70 do dump + PXD Vieira + 4 nomeados = **75**.
Nomeados resolvidos por API (4 pedidos autorizados): gaaGOD 23994179, Chord 19176527, yokithox
9954290, AlockyyZeRa 17192668 (`named_players.json`). **Ainda não foi recolhido nenhum score destes
jogadores** — isso são ~75 recolhas via API e precisa de nova autorização.

**Bug real do limitador corrigido**: no primeiro pedido de cada processo, o pedido de token OAuth
(aninhado em `_auth_header()`) corria depois de `limiter.wait()`, e a chamada à API saía colada ao
token (medido nos logs: 0,67 s entre os 2 primeiros pedidos vs. o mínimo de 1,1 s). Debaixo do limite
oficial de 60/min (5 pedidos em ~4 s), mas violava a regra própria. Agora o header/token é obtido
**antes** de esperar (`api/http.py`), com teste de regressão `tests/test_rate_limit.py` (falha sem a
correção, passa com ela; suite a 47/47). Nota: a mensagem vazia dos primeiros 4 falhados era um
`assert self.run_id` local (sem `start_run`) — zero pedidos saíram.

### Painel de recolhas `osuml panel` (2026-09-23) — visual, com cancelamento e intervalo medido
`python -m osuml panel [--port 8765] [--open]` → http://127.0.0.1:8765 (só localhost; POST exige o
token embutido na página e `Host` local). **Não faz nenhum pedido até carregares em "Iniciar".**
Mostra: fila dos 74 jogadores de `panel_v1.json` (PXD Vieira excluído: já é da recolha agendada),
progresso, **intervalo real início→início de cada pedido** (medido pelo `observer` do `HttpClient`,
token OAuth incluído; abaixo do mínimo −50 ms = violação a vermelho), mínimo medido vs. exigido,
pedidos nos últimos 60 s (máx. 60) e contagem decrescente até ao próximo possível. Botões: Iniciar
(intervalo 1,1/2/5/10/30/60 s; **nunca abaixo de 1,1 s**, recusado no servidor), Cancelar tudo,
Cancelar por jogador (persistente na tabela `panel_jobs`; ao reiniciar, os cancelados voltam à fila
e os feitos não se repetem). Cancelar não deixa sair pedidos novos (`cancel_check` antes de cada
pedido e depois da espera do limitador); um pedido já em voo termina. Por jogador só `best` +
`recent` (nunca `firsts`/`pinned`: um top player teria dezenas de páginas) e **orçamento de 20
pedidos por jogador**. Estimativa: user + best (2 páginas) + recent = **4 pedidos/jogador** × 74 ≈ 300
pedidos ≈ 5,5 min a 1,1 s. `best` para no offset 200 (`SNAPSHOT_MAX_ITEMS` em `collector/scores.py`):
teto **empírico** — 3/3 jogadores (rank 1 e 2 incluídos) deram 100+100+0 itens; o osu-web deixa paginar
`best` sem teto no controlador, por isso não é um limite lido no código. Antes desta correção cada
jogador gastava 1 pedido (1 em 5) numa 3.ª página sempre vazia. Sessão medida: 12 pedidos, 0 violações, intervalo mínimo 2000 ms.
**Bloqueio entre processos** (`api/lock.py`, `data/control/api.lock`, msvcrt/fcntl, liberta-se se o
processo morrer): o `collect` agendado e o painel não correm ao mesmo tempo (senão 2 × 1 pedido/1,1 s
≈ 109/min). O painel recusa iniciar se o `collect` estiver a correr; o `collect` espera até 30 min.
Código: `panel/core.py`, `panel/server.py`; `Cancelled` em `api/http.py`; `collect()` aceita
`user_id` e `snapshot_types`. Testes: `tests/test_panel.py`. **Escolher o intervalo/carregar em
Iniciar é decisão do utilizador** (autorizou a recolha com o painel para acompanhar e cancelar).

### Recolha contínua multi-jogador `osuml poll` (2026-09-23) — tarefa `osuml-poll`
Recolha do painel concluída: **74/74 jogadores, 305 pedidos, 0 erros, 17 422 scores** (75 jogadores
na BD, todos osu). Regras do utilizador para a fase contínua, implementadas em `scheduler/tracker.py`
(tabela `tracked_players`, 10 testes em `tests/test_tracker.py`, mutações verificadas):
- cada jogador é consultado (`recent`) a intervalos **aleatórios de 14–22 h** (sempre < 24 h: o
  `recent` só cobre 24 h) — nunca horas fixas nem rajadas; lotes de **≤ 6 jogadores por execução** com
  **pausa aleatória de 20–90 s** entre jogadores (além do limitador de 1,1 s);
- **`best` só na 1.ª recolha** de cada jogador (`snapshot:best` inexistente); depois só `recent`
  (1 pedido por poll; `user_ttl` de 365 dias para não repetir `/users/{id}`). **PXD Vieira fica de
  fora**: tem a tarefa própria `osuml-collect` (12/12h, `best` com TTL de 7 dias);
- **inatividade ≥ 9 dias** (desde o último score OU desde o início do acompanhamento — período de
  graça para substitutos) → estado `inactive`, `next_poll_at = NULL`, **zero pedidos** a partir daí, e
  substituição por outro jogador **da mesma banda** (`FileCandidates`: 1.º candidatos do painel v1
  com ≥12 dias ativos, depois `osu_user_stats` do dump; 0 pedidos para escolher). Substituto entra
  com `best` + `recent`. Banda `nomeado` **não** é substituída sozinha (só marcada inativa). HTTP 404
  (conta apagada/restrita) = inativo + substituição; outros erros → repete daqui a 2–4 h;
- a atividade mede-se pelo `max(ended_at)` dos scores guardados (o `best` sozinho não chega: um top
  player joga sem bater os seus bests, mas os `recent` entram na mesma tabela). Nota: alguns dos top 20
  têm o último score de maio/julho — se não jogarem em 9 dias de acompanhamento serão substituídos;
- proteções: **teto de 400 pedidos/24 h** (conta todos os processos: a recolha inicial de 305 pedidos
  ainda entra nesta janela até amanhã à tarde), bloqueio entre processos com pausa de 1,1 s ao obter
  o bloqueio (`ApiLock.acquire(settle=...)`), e `python -m osuml poll --pause/--resume` (ficheiro
  `data/control/PAUSE`) para suspender tudo;
- comandos sem pedidos: `osuml poll --status` (estado/bandas/inativos/próximo devido) e
  `osuml poll --dry-run` (o que faria agora). Cada execução acrescenta uma linha a
  `data/logs/poll.jsonl` (a tarefa corre com `pythonw`, sem consola).
- **Tarefa do Task Scheduler `osuml-poll`**: de 30 em 30 min, `--max-start-delay 600`, sem sobreposição
  (`IgnoreNew`), corre com bateria, limite 45 min. Verificado: execução manual real com 0 devidos →
  0 pedidos, log escrito. Primeiro jogador devido: amanhã ~06:08 UTC. **Para desativar**:
  `Disable-ScheduledTask osuml-poll` ou `osuml poll --pause`.

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

### Regra dura: nunca pedir dados novos à API sem autorização explícita do utilizador
O utilizador pediu isto de forma direta: **nunca ultrapassar os limites/políticas da osu!API, e
pedir permissão antes de qualquer pedido novo** (isto cobre sobretudo expandir a recolha a outros
jogadores — a recolha já agendada de "PXD Vieira" de 12/12h já está autorizada e não precisa de
autorização repetida). Já era a prática seguida (perguntar antes de `maps fetch`, antes da pool de
referência, etc.) — fica agora registado como regra explícita, não só bom senso.

## Estrutura

```text
src/osuml/
  config.py               Settings a partir de .env
  cli.py / __main__.py    CLI: collect, status, export, maps {import,fetch,status,export,difficulty,
                          reference-pool,skills}, sync-s3
  api/                    http.py (retry/backoff/logging seguro), rate_limit.py, auth.py, osu.py, lock.py
  collector/scores.py     user → snapshot (best/firsts/pinned) → recent; deteção de lacunas
  storage/                models.py (esquema), database.py (Store), normalize.py, raw.py, s3.py (extra "s3")
  dataset/                export.py, derived.py
  external/               sqldump.py (dumps SQL do data.ppy.sh em streaming)
  panel/                  core.py (fila/cancelar/intervalo medido), server.py (painel local)
  scheduler/              tracker.py (poll contínuo: intervalos aleatórios, inativos, substitutos)
  beatmaps/               acquire.py, parser.py, export.py, difficulty.py, reference_pool.py, skills.py,
                          hitfeatures.py
tests/                    test_collector.py, test_beatmaps.py, test_difficulty.py, test_reference_pool.py,
                          test_skills.py, test_s3.py, test_hitfeatures.py (API/S3 simulados, sem rede)
data/                     raw/ (nunca apagar), processed/<versão>/, osuml.db  — fora do git
docs/design.md            documento de design original
```

Tabelas: `runs`, `api_requests`, `users`, `scores` (PK `score_id`), `score_observations`,
`beatmaps`, `beatmapsets`, `beatmap_files`, `collector_state`, `coverage_gaps`.

## Convenções

- Correr `pytest` antes de dar uma alteração por concluída (108 testes, todos devem passar).
  Testes nunca fazem pedidos reais: usar `httpx.MockTransport`.
- **Nunca apagar `data/raw/`**. Respostas raw são gravadas antes de normalizar.
- Datas guardadas em UTC *naive*.
- Não fazer commit de `.env` nem de `data/`.
- Não fazer pedidos reais à API durante desenvolvimento sem necessidade; preferir `status`
  (0 pedidos) para inspecionar o estado.
- Datasets versionados em `data/processed/<versão>/` com manifest (sha256, git commit, contagens).
