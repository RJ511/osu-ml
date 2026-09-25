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

### Dumps de scores + catálogo de mapas + RunPod (2026-09-24)
- **`osuml dump-scores --tar <..performance_osu_*.tar.bz2>`** (`external/scores_dump.py`, 2 testes): importa
  `scores.sql` (só ruleset osu!, só `user_id`) em streaming para `data/processed/dump_scores/v1/`. Feito:
  `random_10000` = **3 953 159 scores / 10 000 jogadores** (2009-2026, ~10 min) e `top_1000` = **6 483 810 /
  1 000**. **Todos `passed = 1` (o dump não tem fails)** → serve para accuracy dos passes e progresso, não
  para pass/fail (fails só da API/`poll`: ~1-1,5 mil/dia com 75 jogadores). 95 % dos scores têm pp oficial
  (não recalcular pp: não é o alvo e não compensa; só validar uma amostra). Nota: `sync-s3` espelharia
  estes Parquet (bucket privado). Licença do dump: só estatística/uso privado.
- **`osuml map-catalog {plan,bundle,run,merge,all}`** (`beatmaps/catalog.py`, 10 testes): atributos por
  (mapa, mods relevantes DT/HT/HR/EZ/HD/FL; NC=DT) — stars/aim/speed (rosu-pp), **reading** (port do lazer),
  density, AR/CS/OD/HP... — dos 50 000 mapas mais jogados (cobrem 86,7 % dos scores; 142 890 mapas no total).
  `plan` → `data/processed/catalog/v1/plan.json` (**267 457 pares** com `--min-plays 3`; 149 mil com 10; 90 mil
  com 25); `bundle` → só os `.osu` do plano; `run` → calcula (`--shard I/K`, partes de 2000 mapas, **retomável**);
  `merge` → `map_attributes.parquet`. Custo medido **~65 ms/par** ⇒ ~90 min com 3 processos locais (o cálculo do
  Reading domina). O processo local `all` foi **parado** a pedido do utilizador (guardava tudo em memória; perdeu-se
  ~20 min) e o trabalho vai para um **pod RunPod (só CPU)**. `__main__.py` ganhou `if __name__ == "__main__"`
  (senão o multiprocessing no Windows re-executa o CLI).
- **RunPod (pod, não serverless)**: pasta `data/runpod/` = `osuml_src.zip` + `runpod_catalog.sh` (cópia de
  `scripts/`) + `plan.json` + `osu_subset.tar.gz`. No pod: `bash runpod_catalog.sh` (instala `uv`, Python 3.13,
  `osuml[parquet,difficulty]`, corre com `nproc` processos, gera `catalog_parts.tar.gz`). Transferência com
  `runpodctl send/receive` ou `scp`/`rsync` (docs.runpod.io/runpodctl/transfer-files). De volta ao PC: extrair
  as partes para `data/processed/catalog/v1/parts/` e `osuml map-catalog merge --plan .../plan.json`.
  O pod **nunca** faz pedidos à osu!API nem recebe credenciais; dados do dump só privados.

- **Catálogo v1 concluído no RunPod (2026-09-24)**: pod só de CPU (16 vCPU, 251 GB RAM, Ubuntu 20.04; SSH direto
  `root@<ip>:<porta>` com a chave `~/.ssh/pod`, sem passphrase; o proxy `ssh.runpod.io` não suporta `scp`). **Resultado**:
  `data/processed/catalog/v1/map_attributes.parquet` = **267 423 linhas / 49 994 mapas** (faltam 6 dos 50 000: 2 fora do
  dump + 4 que excederam 30 s: 1029976, 2571858, 2628991, 4679228), ~107 mapas/s no pod (**~6-8 min** em vez dos ~90 min
  estimados localmente). Sanidade: DT > nomod em ★ em 37 195/37 196 mapas; **extremos de mapas degenerados** (★ máx. 222,
  aim 133, reading 366 578, com AR 0) → usar log/clipping nos modelos. `catalog_parts.tar.gz` + `plan.json` em
  `data/processed/catalog/v1/`; `data/runpod/` (443 MB) pode apagar-se. O pod foi **parado** (`runpodctl stop pod`
  a partir de dentro, usando as variáveis do PID 1); um pod parado ainda pode cobrar disco — **apagar no RunPod** se
  não for reutilizado.
- **Lições do RunPod** (bugs reais): (1) `drain` esperava sempre pelo futuro mais antigo, por isso **um mapa lento bloqueava
  os 16 processos** → `compute_map` tem agora limite de **30 s por mapa** (`SIGALRM`, só POSIX; o teste só corre em
  Linux) e o mapa é marcado como falhado; (2) `pkill -f "osuml map-catalog"` numa shell cujo comando contém esse texto
  **mata a própria shell** → usar `kill -TERM -<pgid>`; (3) lançar dois trabalhos no mesmo `--out-dir` faz as partes
  colidirem (`part_all_N`): a retoma (`run` outra vez) recalcula o que faltar — foi assim que se recuperou; (4) `ssh ... &`
  a partir do Bash pendura: usar `ssh -n -f` + `setsid nohup ... < /dev/null`; (5) **Git Bash converte argumentos
  `/root/...` em caminhos Windows** → `MSYS_NO_PATHCONV=1`; (6) no Windows dois servidores podem ligar-se à mesma
  porta (SO_REUSEADDR) e o antigo continua a responder → matar todos os processos antes de relançar.
- **Janela de progresso** (`scripts/progress_window.py`, preferência do utilizador — ver memória): lê `progress.json`
  (`_write_progress` em `catalog.py`) localmente (`--file`) ou por SSH (`--ssh ... --remote-progress ...`) e serve a barra em
  http://127.0.0.1:8770. Tarefas longas novas devem escrever esse formato e abrir a janela no browser pane.

### Validação do pp + baseline de accuracy (2026-09-24) — `osuml analyze {pp-check,acc-baseline}`
Código em `src/osuml/analysis/` (`ppcheck.py`, `accuracy_baseline.py`), `progress.py` (escreve `progress.json` para as
janelas de progresso), 4 testes em `tests/test_analysis.py`; extra `lightgbm` instalado no venv (numpy/scipy vêm com ele).
Saídas em `data/processed/analysis/{pp_check,acc_baseline}/v1/`. Tudo local, 0 pedidos, ~1 min cada.
- **pp-check** (5 000 scores legacy amostrados de 13 912 candidatos, sem RX/AP/TD/speed_change, `lazer=False`): o nosso pp
  (rosu-pp 4.0.2) fica **sistematicamente abaixo do pp oficial do dump**: erro médio **−7,8 %**, mediana |erro| 8,2 %, P90 20,7 %,
  só 6,9 % dentro de 1 % e 32,9 % dentro de 5 %, mas **correlação 0,995**. Pior em EZ+HD (ex.: 24,6 oficial vs 8,1 nosso).
  Hipótese (só parcialmente suportada): o pp oficial de 2026 já inclui a skill **Reading**, que o rosu-pp 4.0.2 não tem
  (excesso oficial/nosso: +7-8 % nos 3 primeiros quartis de Reading relativo, +14,9 % no último; Spearman só 0,16 e −0,23 com AR).
  **Consequência**: usar sempre o `pp` oficial do dump, nunca o nosso, como variável; as escalas aim/speed/reading do catálogo
  são relativas e não são afetadas.
- **acc-baseline** (split temporal: histórico 2023-01→2024-12 só para features do jogador; treino 2025-01→06; teste 2025-07→2026-09;
  ≤100 scores/jogador; jogadores com ≥20 scores de histórico; só passes): treino 156 455 / teste 189 144 linhas, 3 710 jogadores
  com histórico. Alvo: média 0,9135, desvio 0,0824. **MAE** (fração; 0,01 = 1 ponto): média global 0,0588; **média do jogador 0,0493**;
  LightGBM só com o mapa 0,0514 (pior que a média do jogador!); **LightGBM completo 0,0415 (−15,8 % vs média do jogador; R² 0,44
  vs 0,21)**. Ganho em todas as faixas de ★ (p. ex. 6-8★: 0,0420→0,0344). Features mais importantes: `p_mean_acc` (30 %),
  `gap_stars` (12 %), `gap_speed` (11 %) — a distância entre a dificuldade do mapa e o nível habitual do jogador conta mais que o mapa em si.
  **Limites**: 15,6 % das linhas da janela ficaram sem catálogo (mapas fora dos 50 mil mais jogados), sem jogadores novos nem
  hold-out de jogadores, sem fails (o dump não os tem), features do jogador fixas (não se atualizam durante o teste).
- **acc-baseline v2** (hold-out de 20 % dos jogadores fora do treino + ablações; `data/processed/analysis/acc_baseline/v2/results.json`,
  54 s): treino 125 569 linhas / 2 354 jogadores. **Jogadores vistos** (n=151 971): média do jogador MAE 0,0494 → LightGBM completo 0,0418
  (**−15,4 %**, R² 0,44). **Jogadores nunca vistos** (n=37 173, 692 jogadores): 0,0486 → 0,0438 (**−10,0 %**, R² 0,35) — generaliza a
  jogadores novos *com histórico*, mas menos. **Ablações (dentro do ruído, diferenças < 1 % relativo)**: sem features de Reading
  0,0420 (vistos) / 0,0434 (nunca vistos) vs completo 0,0418 / 0,0438 → **o Reading não acrescenta nada mensurável à accuracy dos
  passes**; sem as distâncias mapa-jogador (`gap_*`) 0,0415 / 0,0437 → também não acrescentam (as árvores recuperam-nas das features
  brutas). O Reading pode continuar a importar para **pass/fail**, que o dump não permite testar (sem fails). Uma só divisão temporal, sem
  repetições: diferenças de ~0,0003 não são conclusivas.
- Janelas de progresso: uma por processo (`scripts/progress_window.py --file <progress.json> --port 877x`), abertas no browser pane.

### Painel principal 8765: separador "Tarefas" + faixa de progresso (2026-09-24) — decisão do utilizador
**Todas as barras de progresso vivem no painel principal (`osuml panel --port 8765`)**, não em janelas separadas. `src/osuml/jobs/`
(`core.py` = `JobManager`, `server.py` = página; 6 testes em `tests/test_jobs.py`): descobre qualquer `progress.json` /
`*.progress.json` / `progress_*.json` sob `data/processed` (mesmo de tarefas lançadas num terminal), mostra barra, %, feito/total,
velocidade, tempo restante, log e estado (em curso / concluída / erro-cancelada / interrompida = sem sinal há 2 min e o `pid` já
não existe); **faixa no topo do painel, visível em qualquer separador**, com as tarefas em curso (clicar abre "Tarefas").
**Botões**: "Lançar" só para modelos pré-definidos (`default_templates`: importar playcount random/top, `playcount-check`,
`pp-check`, `acc-baseline`; `requires` = ficheiros necessários, `after` = tarefas de que depende) — nunca comandos arbitrários; corre
`python -m osuml ...` num subprocesso (log em `data/logs/jobs/`, progresso em `data/processed/tasks/`); "Cancelar" termina o
processo e filhos (`taskkill /T /F` no Windows — **nunca `os.kill`, que no Windows termina o processo**) usando o `pid` do
próprio `progress.json`. O progresso é acessório: `Progress._write` tolera o `os.replace` falhar no Windows (leitor com o ficheiro
aberto) em vez de deitar a tarefa abaixo (bug real que matou uma importação). `scripts/progress_window.py` fica só para ver um pod por SSH.

### Tentativas do dump (`osu_user_beatmap_playcount`) como fonte de "não passou" — sem API (2026-09-24)
`osuml dump-table --tar <dump> --table osu_user_beatmap_playcount` (`external/table_dump.py`, progresso por bytes lidos) importou
**16 172 857 pares (jogador, mapa)** (random_10000: 6 839 522; top_1000: 9 373 390) para `data/processed/dump_tables/v1/`. Cruzados com os
passes de `scores.sql` por `osuml analyze playcount-check` (6 s; resultado em `data/processed/analysis/playcount_check/p0924-1424/`):
- **`playcount ≥ passes` em 99,95 %** dos 7 428 219 pares com score ⇒ a tabela conta tentativas (incluindo passes). 213,6 M tentativas vs 10,4 M passes.
- **8 744 638 pares (54 % do playcount) têm tentativas e nenhum passe** (média 4,96 tentativas) ⇒ **exemplos negativos ("tentou e nunca passou") para
  11 mil jogadores, sem API** — o que faltava para modelar pass/fail e o "limite" de cada jogador. Positivos: 7,4 M pares com passe.
- **Limites (não tratar como fails exatos)**: o playcount inclui *quits*/retries e não tem datas; o `scores` guarda poucos passes por par (1,4 em
  média, 75 % dos pares só 1), por isso `playcount − passes` (mediana +6) sobrestima fails. Alvo utilizável: **"passou alguma vez?"** e **tentativas até
  passar**, não contagens de fails nem séries temporais.
- Próximo passo proposto: modelo P(passar alguma vez | jogador, mapa) com hold-out de jogadores; features do jogador calculadas com metade dos mapas e
  alvo na outra metade (evita fuga); mapas fora do catálogo ficam de fora.

### Modelo P(passar alguma vez | jogador, mapa) — resultados com 25 % dos jogadores (2026-09-24)
`osuml analyze pass-model` (`analysis/pass_model.py`, 8 testes; extra `ml` no `pyproject.toml`; `scripts/runpod_pass_model.sh`; entradas em
`data/processed/analysis/pass_model/inputs/`). **Ensaio local com 25 % dos jogadores** (1 697 jogadores, 1,24 M pares, 81 s; resultados em
`.../pass_model/smoke2/`): teste em 342 jogadores nunca vistos (246 k linhas). **AUC**: só mapa 0,695; + perfil do jogador (A) **0,753**; + taxa de passe (B)
0,783; + estatística do mapa (C) 0,767; baselines: taxa de passe do mapa 0,698, do jogador 0,666; rótulos baralhados 0,523 (acaso). Calibração boa
(ECE 0,02: previsto 0,74 → observado 0,73). Por fonte: aleatórios 0,778, top 0,676. Estável por nº de tentativas (0,78 com 1 tentativa e com 20+; a taxa de
passe sobe de 29 % com 1 tentativa para 86 % com 20+, por isso as tentativas NÃO são feature). Features mais importantes (A): `gap_stars`, `p_n_pass`
(atividade do jogador — cuidado), `n_objects`, `gap_speed`. **Fora do tempo** (17 jogadores da API, 1 263 jogadas depois do snapshot 2026-09-01): A 0,59 no total,
**0,65 em jogadas sem mods (IC 0,61-0,68) e 0,49 em jogadas com mods** (o modelo só vê atributos nomod; o playcount não diz que mods se usaram); mapas novos 0,77,
nunca passados 0,59, já passados 0,51. **PXD Vieira / gaaGOD (não estão nos dumps, só dados da API, perfil sem o mapa-alvo)**: por par 0,67/0,74 e 0,80/0,84 (A/C)
com IC largos (poucos negativos); por jogada recente (n=60 e 23) ≈ 0,5-0,6, inconclusivo. **Leitura correta**: mede "alcançabilidade" relativa ao perfil,
não a probabilidade de passar hoje (não vê ritmo de evolução, semelhança com o estilo do jogador, mods, forma do dia, sorte). Bug apanhado: hashes com sementes
diferentes eram correlacionados (`_hash01` → splitmix64), o que deixava a validação vazia depois de amostrar.
- **RunPod**: `pod-action start` no pod existente (1u9bsup19laeo5, cpu5c 16 vCPU, 0,56 $/h, EU-RO-1) falhou: "not enough free vcpu on the host machine" (o pod fica
  preso ao host). Nada foi cobrado. Alternativas: repetir mais tarde ou criar pod novo. O painel passou a mostrar **tarefas remotas** (`data/control/remote_tasks.json`,
  lidas por SSH; barra com etiqueta ☁, cancelar por SSH; 2 testes).
- **Objetivo do utilizador (o que importa)**: recomendar mapas que vão ao encontro do que o jogador procura — p. ex. para melhorar o **speed**, mapas que o desafiem
  em speed, que a princípio consiga fazer mas que melhorem essa skill. O modelo pass/fail é só um filtro de "alcançável"; falta lógica por eixo, semelhança
  de estilo, ritmo de evolução e validação com o próprio utilizador.

### Recomendador: definições do utilizador e primeiros resultados (2026-09-24)
**Decisões do utilizador**: (1) o pod RunPod só se liga se houver trabalho que precise dele agora; (2) o eixo (ex.: speed) era só um exemplo — o pedido pode ser
**uma ou várias skills**; (3) **"alcançável" = o jogador chega a ≥ 88 % de accuracy (93 % seria melhor)** e quer-se medir quão correta é essa assunção;
(4) "estilo parecido" de duas formas: (a) jogadores com o mesmo perfil (se um joga um mapa, o outro provavelmente também quer) e (b) mapa a mapa pelos atributos;
(5) **ritmo de evolução** = ligado ao pp / posição no ranking ao longo do tempo (`rank_history`, `monthly_playcounts`) — **por agora ignorar por causa dos pedidos à
API; ANOTADO**: os 75 jogadores acompanhados **já têm `rank_history` (90 dias) e `monthly_playcounts` guardados em `users.raw`** (dos pedidos já feitos), por isso dá para usar
mais tarde **sem pedidos novos** (só falta usá-los; jogadores fora dos 75 exigiriam pedidos → pedir autorização).
- **`osuml analyze reach-model`** (`analysis/reach_model.py`; alvo por par = melhor accuracy entre os passes ≥ 0,88 / ≥ 0,93; ensaio 25 % em `.../reach_model/r25/`,
  131 s, 342 jogadores de teste nunca vistos, 246 k linhas): **AUC 0,79 (≥88 %) e 0,82 (≥93 %)**, calibração excelente (ECE 0,008 e 0,005), melhor que o alvo "passar" (0,75).
  Fiabilidade do modelo A para ≥88 %: previsto ≥0,5 → observado 64 %; ≥0,7 → 76 %; ≥0,8 → 82 %; ≥0,9 → 86 % (ligeiramente otimista no topo, pessimista em 0,7);
  só ~10 % das linhas chegam a ≥0,7 (base 37 %; para ≥93 % a base é 27 % e só 4 % chegam a ≥0,7 → 76 %). Por distância do mapa ao P90 de estrelas do jogador
  (≥88 %): abaixo do nível 39-47 %; **0..+0,5★ acima só 19 %; +0,5..+1★ 11 %; >+1★ 8 %** ⇒ a "esticada" tem de ser **por eixo** (ex.: speed) mantendo as estrelas totais ao
  nível do jogador. Features mais importantes: `p_mean_acc`, `gap_speed`, `p_n_pass`, `gap_stars`. Aleatórios AUC 0,82 vs top 0,70. **Limites**: melhor jogada entre TODAS as
  tentativas, sem mods, sem forma do dia; ainda sem teste fora-do-tempo para os limiares de accuracy.
- **`osuml analyze similarity`** (`analysis/similarity.py`, 20 s, 800 jogadores de teste com ~540 pares escondidos cada, jogadores/mapas do catálogo): recall@50 / @200 —
  popularidade 0,031 / 0,083; **jogadores parecidos (user-CF) 0,070 / 0,163**; item-CF 0,025 / 0,066; **mapa a mapa (perfil médio de atributos) 0,004 / 0,014**;
  híbrido user-CF + conteúdo 0,070 / 0,162 (cauda 0,020 / 0,077, melhor). Como só há ~540 escondidos, o teto de recall@50 é ~0,09: user-CF acerta ≈ 76 % da lista de 50
  (popularidade ≈ 33 %). **Conclusão**: para "gostar/querer jogar" usar jogadores parecidos; os atributos do mapa servem melhor como filtro (alcançável, eixo desafiado) e
  para explicar/cold start do que como sinal de gosto.
- **Desenho proposto do recomendador** (por implementar): pedido = conjunto de eixos + limiar (88/93) → candidatos do catálogo → (1) alcançável: P(≥88/93) do modelo `reach`
  acima de um mínimo; (2) desafio: exigência nos eixos pedidos acima do nível do jogador e os restantes ao nível; (3) estilo: score user-CF (jogadores parecidos) + conteúdo
  como desempate; (4) novidade: mapas ainda não jogados; (5) mais tarde: ritmo de evolução (`rank_history`) a ajustar o tamanho da esticada. Validação real = o utilizador
  experimentar e dar feedback ("serve / não serve").

### Recomendador v0 (2026-09-24) — `src/osuml/recommend/`, painel Explorar → jogador → "Recomendar mapas"
Objetivo do utilizador: o jogador escolhe **só a(s) skill(s)** (Aim/Speed/Stamina/Reading; sem estrelas nem limiares) e recebe mapas
que o desafiem nessas skills, que a princípio consiga fazer e do estilo de jogadores parecidos. **0 pedidos à API.**
- **Índice** (`osuml recommend build-index --sample-pct 70`, → `data/processed/recommend/index/`: `index.npz`, `cf_matrix.npz`,
  `cf_users.npy`, `labels.parquet`, `meta.json`) e modelos LightGBM de alcançabilidade (`recommend/models/`, treinados numa amostra de
  25 %; P(accuracy ≥ t) para t ∈ 85/88/90/93/95/97 %). `Recommender` (`recommend/core.py`) lê ambos; `recommend(user_id, skills, n)`.
- **Alcançável** = P(≥ 88 %) ≥ 0,40 (mostra também ≥ 93 %). **Estilo** = filtragem colaborativa entre jogadores (cosseno, 50 vizinhos)
  — foi a melhor das variantes testadas; só conteúdo (mapa↔mapa) ficou fraca, híbrido só melhora ligeiramente a cauda.
  **Desafio** = delta (nota da skill do mapa − nível do jogador, P90 dos top-200 pp), pico em +6; eixos não escolhidos não podem
  subir mais de 4 (limite 8). Nível do jogador = P90 das notas dos melhores passes.
- **Tipos**: `novo`, `tentar_de_novo` (tentado e nunca passado) e `rejogar` (já jogado, mas accuracy prevista ≥ +3 pontos e pp
  estimado ≥ +10 % via curva acc→pp medida com rosu-pp; curva em `core.pp_factor`). Decisão do utilizador: repetir mapas é uma **opção**,
  não tem de aparecer muito. Na prática aparecem raramente (PXD: 2 mapas; gaaGOD: 1) por **viés de seleção**: os melhores scores
  guardados têm accuracy acima da previsão típica (mediana 0,977 vs 0,946), pelo que só se repete quando há folga clara.
- **Feedback**: botões "Serve / Não serve" → tabela `recommendation_feedback` (user, mapa, veredicto, tipo, skills, score). Só grava; ainda
  não é usado para treinar nada.
- **Limites**: atributos só nomod (mods só entram no perfil, não na previsão por mods); modelos numa amostra de 25 % (retreino com tudo
  → RunPod só se compensar, dizer preço antes); a hipótese "≥ 88 %" só validada com hold-out de jogadores, falta validação fora do
  tempo com jogadores da API; **ritmo de evolução** (rank_history/monthly_playcounts) **anotado, não implementado** — os campos já estão
  guardados localmente para os 75 jogadores (0 pedidos novos), mas para outros jogadores exigiria pedidos à API (perguntar antes).
- Testes: `tests/test_recommend.py` (7); suite total 164 passam, 1 só corre em Linux. UI verificada no painel (8765): 20 sugestões
  para PXD Vieira com Speed, feedback gravado (linha de teste apagada).

### Treino completo no RunPod + recomendador v1 + aplicação autónoma (2026-09-24)
**Dados** (sem API; autorizados pelo utilizador): 5 dumps `random_10000` novos (2026_04_01, 05_01, 06_01, 07_13, 08_01) + `top_10000` de 2026_09_01,
descarregados **diretamente no pod**. Cada dump aleatório é uma amostra **diferente** (sobreposição de só ~20-40 jogadores entre dumps) ⇒ **60 564
jogadores únicos** nos scores aleatórios + 10 000 do top. `top_10000` = 56,6 M scores. Parquet tratados guardados em `data/processed/dump_scores/v1/`,
`dump_tables/v1/` (9 playcount); manifestos conferem (linhas e jogadores). O dump de `.osu` (`2026_09_01_osu_files.tar.bz2`) **já não está** na raiz do repo
(retirado, não pelo assistente); o pod tinha uma cópia e as etiquetas foram extraídas lá.
- **Catálogo v2** (`data/processed/catalog/v2/`): plano = mapas com scores **+ mapas só tentados (playcount)**: sem estes, os mapas que ninguém da amostra
  passou (os mais difíceis) ficavam fora e o treino enviesava para mapas fáceis. 233 410 mapas no plano, **152 268 osu!standard calculados** (633 476 linhas
  mapa×mods); 81 142 excluídos (taiko/catch/mania — o playcount de osu! inclui converts; amostra de 300: 300/300 não-std). `choose_pairs(..., map_files=)`,
  `map-catalog plan --playcounts`, `OSUML_PROGRESS_FILE`. Guarda do pipeline: só falha se >50 % falharem (rosu-pp em falta falha tudo em silêncio).
- **Treino** (`scripts/pod_pipeline.py`, `runpod_full_train.sh`; pod cpu5g 16 vCPU 64 GB, 0,736 $/h, ~100 min ⇒ ~1,2 $): downloads a **blocos paralelos**
  (a origem limita o `top_10000` a ~1 MB/s por ligação; 24 ligações ⇒ ~21 MB/s), `dump-scores --workers` (parsing paralelo).
  **Pass/fail** (sem `top_10000`; 24 M pares, 37 937 jogadores, 7 519 de teste nunca vistos): AUC A **0,791** (era 0,753 a 25 %), C 0,801, B 0,820, M 0,732;
  repetições 0,7907±0,0016; rótulos baralhados 0,533; top players 0,703 (era 0,676); API fora-do-tempo 0,60. **Alcançável** (o do recomendador; **com**
  `top_10000`; 38,2 M pares, 46 739 jogadores, 9 258 de teste, treino 10 M linhas): AUC **0,793 (≥85) / 0,804 (≥88) / 0,813 / 0,832 (≥93) / 0,847 / 0,869 (≥97)**,
  ECE 0,007-0,009. **acc-baseline** (todos os dados): LightGBM completo −16,6 % MAE vs média do jogador (vistos) e **−13,95 % (nunca vistos; era −10,0 %)**;
  sem Reading ≈ igual (−16,5/−14,0): **Reading continua sem acrescentar à accuracy dos passes**; `gap_*` ajudam só nos nunca vistos.
  Resultados em `data/processed/analysis/pod_full/{results,logs}`. Não se fez ablação do `top_10000` (o reach já o inclui; o pass não).
- **Validação com jogadores da API** (`osuml analyze reach-api-check`, BD local, 0 pedidos; `analysis/reach_api_check.py`): **o modelo novo NÃO é melhor que o
  antigo nestes jogadores** (AUC temporal ≈ 0,72 nos dois; pior nas metades enviesadas) e é **mais conservador** (previsto médio 0,25 vs observado 0,53 em ≥88 %).
  **Ordena bem** (monótono) mas **subestima**: previsto 0,2-0,3 ⇒ observado 0,62; 0,4-0,5 ⇒ 0,71 (temporal, n=2 874, 71 jogadores). Causa provável: a BD só guarda
  best+recent (viés de seleção) e o nº de passes do perfil é bem menor que nos dumps. Medido também: quando o modelo "esperava" 80-84 %, esses jogadores
  tinham **mediana de 95 % nos passes** e 66 % chegavam a ≥ 88 % (PXD Vieira: esperado 85 %, real 94 %).
- **Regra do utilizador (2026-09-24): "abaixo de 88 % não se aprende; por volta de 93 % aprende-se" ⇒ nenhuma sugestão pode ter accuracy esperada < 88 %.**
  Antes o filtro era P(≥88 %) ≥ 0,30 e a "accuracy provável" (mediana do modelo bruto) ficava nos 80 — incoerente. Agora: (1) **calibração** dos modelos para jogadores da API
  (`analysis/reach_calibration.py`, `osuml analyze reach-calibrate` → `models/calibration.json`, incluída no pacote): `logit(p_cal) = a + b·logit(p_bruto)` por limiar, ajustada a
  2 874 pares fora-do-tempo de 71 jogadores; **validação cruzada por jogador**: ECE 0,28 → 0,03 e Brier 0,298 → 0,212 em ≥ 88 % (a ordenação não muda; é monótona);
  (2) o score favorece a zona de aprendizagem: `0,35·desafio + 0,30·P(≥93 %) + 0,15·P(≥88 %) + 0,20·estilo`.
  **Comparação medida (lista antiga vs calibrada)**: a lista antiga reproduz-se exactamente; das 160 sugestões, 102 mantiveram-se, mas a subida dos números (P88 mediana 36-43 % → 61-72 %,
  accuracy 81-85 % → 92-95 %) foi **quase toda re-escala**: avaliada com o modelo bruto, a lista nova continua com P88 27-45 % e accuracy 79-85 %. Dizer "corrigi" foi exagero.
  **Decisão do utilizador (opção 1)**: critério **estrito** = accuracy esperada ≥ 88 % pelo modelo **bruto** (`MIN_REACH_NEW = 0,50` sobre P(≥88 %) bruta; `MIN_EXPECTED_ACC` também para repetir).
  Só se houver **menos de 10** sugestões seguras (`MIN_SAFE_ITEMS`) se completa (até `n`) com as que só o modelo **calibrado** aceita; as seguras ficam sempre primeiro. Cada item traz
  `tier` (`seguro`/`provavel`), `acc_raw`/`acc_cal`, `p88_raw`/`p88_cal`; as UIs mostram "Nível". `LightGbmPredictor.predict_both` devolve (bruto, calibrado); `predict` = calibrado.
  Resultado (PXD Vieira / gaaGOD, 4 skills): Aim 20 seguras (ambos), Stamina 11 (PXD) / 8+12 (gaaGOD), Reading 18 (ambos), **Speed só 3 seguras** (desafio +1,4…+1,8) + 17 prováveis
  (desafio até +6): subir Speed a valer com ≥ 88 % esperado quase não tem mapas. **Limite**: a calibração usa mapas que o jogador escolheu jogar (viés de seleção); a verdade está entre o bruto e o
  calibrado — o feedback "Serve / Não serve" é o que o decide. Modelo cru: `LightGbmPredictor(calibration=False)`.
- **Redesenho final (2026-09-24, pedido do utilizador): accuracy À TILLERINO + P(passar) — substitui as camadas bruto/calibrado acima.** O utilizador esclareceu que "accuracy" é a
  accuracy que se faz **quando se passa o mapa** e que interessa também **não morrer no mapa** (P(passar) ≥ 80 %). Os modelos `reach` respondiam a outra pergunta (P(melhor tentativa passa E ≥ X %),
  falhas = 0), por isso o "acc esperada" deles misturava passar com accuracy (nos jogadores da API "esperado 84 %" ⇒ mediana real dos passes 95 %). Agora o recomendador usa duas quantidades:
  (1) **P(passar)**: `pass_model_A` (dumps; AUC 0,79) **calibrado** com jogadores da API (`analysis/pass_calibration.py`, `osuml analyze pass-calibrate` → `models/calibration_pass_acc.json`;
  `logit(p_cal) = 1,66 + 0,85·logit(p)`; validação cruzada por jogador: ECE 0,35 → 0,018, Brier 0,317 → 0,188; previsto 0,75 → observado 0,74, 0,85 → 0,83; sem calibração previsto 0,49 → observado 0,83);
  (2) **accuracy esperada se passar**: novo modelo `acc_pass_A` (`analysis/acc_model.py`, `osuml analyze acc-model`; LightGBM `regression_l1` = mediana do melhor passe do par; treinado no pod com 18,8 M pares
  que passaram / 46 738 jogadores, teste em 9 257 nunca vistos: **MAE 3,7 pontos, R² 0,45**; baselines: média do jogador MAE 4,9, mediana global 5,8). Nos jogadores da API quase não tem viés
  (previsto 95,6 %, real 94,5 %; deslocamento −0,7 pts aplicado); prevista 90-93 ⇒ 82 % chegam a ≥ 88 %; prevista 93-95 ⇒ 90 %; prevista 88-90 ⇒ só 40 % (é a mediana).
  **Regra**: P(passar) ≥ 0,80 **e** accuracy esperada ao passar ≥ 0,88 (ideal ~0,93; o score favorece ~93 %: `0,35·desafio + 0,25·aprendizagem(acc~93 %) + 0,20·P(passar) + 0,20·estilo`); se houver < 10
  sugestões assim (`MIN_SAFE_ITEMS`), completa-se com P(passar) ≥ 0,70 (`arriscado`; a accuracy continua ≥ 88 %). Tiers: `seguro`/`arriscado`. Campos: `p_pass`, `acc_pass`, `p_pass_raw`, `acc_pass_raw`.
  `PassAccPredictor` (calibration=False dá o bruto). `LightGbmPredictor`/reach ficam só para análise (`reach-api-check`, `reach-calibrate`). Pacote: `pass_model_A.txt`, `acc_pass_A.txt`, `calibration_pass_acc.json`.
  **Resultado**: as 8 listas (PXD Vieira/gaaGOD × 4 skills) dão 20 sugestões `seguro` cada, P(passar) 80-84 %, accuracy se passar mediana 93-96 %, mínimo 90 %; desafio Speed +2,1…+6,1 (PXD).
  **Limites**: a calibração usa mapas que o jogador escolheu jogar (viés otimista); **não usa o historial do próprio jogador no mapa** (ex.: 176960 — o PXD falhou 3 vezes a 22/09 com 88-93 % de accuracy até
- **Comparação com a realidade (jogadas do PXD Vieira a 24/09, 21 mapas / 65 plays; perfil só até 23/09) e perfil de FORMA ATUAL** (`analysis/profile_form.py`, pedido do utilizador: mais peso às jogadas
  recentes, só passes, com cuidado com as fáceis). Achados: P(passar) previsto 81 % = observado 81 % (17/21 mapas), mas **por tentativa só 35 % (23/65; 1.ª tentativa 43 %)** — o "passar" do modelo é "acaba por passar";
  23 das 42 falhas foram a ≥ 88 % (reinícios, inferência). Accuracy se passar: prevista 96,6 % vs real 94,5 % (viés +2,8) e, nos mapas > +3 acima do nível, 95,8 vs 91,3 (+4,3; os 4 mapas que já estavam nas
  listas: 95-96,6 previsto, 84,8-91,5 real). Nos 73 jogadores da API o viés da accuracy é ≤ 1,3 pontos e **não cresce com a exigência**; o P(passar) é ~7 pts optimista a +3..+6 acima do nível e ~12 a > +6.
  Causa provável no PXD: o perfil era o pico (185 dos 249 passes com > 6 meses; mediana 147 pp vs 113 pp nos recentes; gama recente ~105-200 pp).
  **Perfil de forma**: 1 passe por mapa (maior pp); peso = recência (`0,25 + 0,75·0,5^(idade/45 d)`, idade desde o último passe do jogador) × esforço (chão = P25 e teto = P95 dos passes dos últimos 90 dias, até 60;
  pp ≤ chão pesa `EASY_W`, sobe até 1 a meio da gama); estatísticas ponderadas (p50/p90, acc média, DT/HD/HR; "máx" só com peso ≥ 0,22); `k` e vetor iguais ao de treino ⇒ **mesmo modelo, sem re-treino**.
  Medido (perfil antes do corte): PXD 24/09 viés da accuracy base +2,8 → recência +1,4 → forma(EASY_W 0,5) **+1,8** → forma(0,2, "esforço forte") +2,0; população (73 jogadores, 3 531 pares) AUC 0,743 → 0,747 (recência)
  / **0,745 (forma 0,5)** / 0,737 (forma 0,2); MAE 3,2 → 3,1. Adoptado **`EASY_W = 0,5`** (a versão forte piorou a população: empurra o perfil para cima). Recalibrado com o mesmo perfil (`pass-calibrate` v2:
  `logit(p_cal) = 1,52 + 0,86·logit(p)`, ECE 0,32 → 0,019 em CV por jogador; acc deslocamento −1,05 pts). `Recommender(profile_mode="form"|"recency"|"base")`. Efeito prático pequeno: níveis do PXD 76/70/71,9/68,1
  (antes 75/69,6/71,2/67,4), Speed +1,5…+4,3. Pacote atual: `…0e69cefab468-passacc2.zip` (substitui `passacc1`). **Limite**: 8 mapas de desafio num só jogador não provam a causa; é preciso mais dias de dados.
- **BUG DE DADOS corrigido (2026-09-25) — invalida conclusões anteriores sobre jogadores da API.** (1) A API devolve `passed: false` em scores **legacy** (do stable, `legacy_score_id`) com rank A..XH; 3 002 scores de 31 dos 73 jogadores
  estavam a entrar como "falhas" (e todos eram posteriores a 01/09 ⇒ contaminavam a validação temporal e a calibração). Fix em `storage/normalize.effective_passed` (legacy com rank ≠ F ⇒ passe) + UPDATE na BD (cópia
  `data/osuml.db.bak-2026-09-25`); lazer: `passed` é fiável (falhados = rank F, 2 060). (2) **O stable não envia falhas**: nos jogadores de stable só há passes ⇒ a "taxa de passar" fica inflacionada (85 % vs ~65 % no lazer).
  Por isso `pass_calibration` calibra P(passar) **só com pares com tentativas do lazer** (1 684 pares, 48 jogadores; `a=1,92, b=0,91`; ECE 0,37 → 0,025 em CV) e a accuracy usa todos os passes (n=3 039; viés +3,1 pts; deslocamento −1,6).
  As conclusões "o modelo subestima muito nos jogadores da API" e as tabelas de fiabilidade anteriores estavam contaminadas por isto.
- **Pontos de falha** (`analysis/fail_points.py`, `osuml analyze fail-points`, resultados em `data/processed/analysis/fail_points/v1/`): 2 060 falhas reais (lazer, rank F, 35 jogadores); 1 560 analisadas (580 dos 805 mapas estão no bundle v1).
  **Progresso** = (great+ok+meh+miss)/máx great — validado: correlação **0,95** com a duração real da jogada (o "62 % a ≥ 90 %" inicial era só um artefacto dos scores legacy mal marcados). **Morte vs reinício** (pedido do utilizador) com o modelo de HP do
  lazer (`DrainingHealthProcessor`/`OsuHealthProcessor`): vida mínima de um jogo perfeito 0,99/0,90/0,40 (HP 0/5/10); dano por miss = 0,03+pen (0,03/0,125/0,20), meh 0,028, ok 0,019, tick grande falhado 0,015+pen (0,02/0,075/0,14); **só
  pode ter morrido se o dano ≥ vida mínima** (`reinicio_certo` se não), `morte_provavel` se ≥ 1,5×. HR ×1,4 no HP, EZ excluído. **Validação independente**: reinícios certos (908) caem em trechos fáceis (percentil de intensidade da janela de 10 s
  0,24; mediana 7 % do mapa, 1 miss), mortes (623) em trechos intensos (0,64; mediana 24-49 % do mapa, 5-10 misses). PXD Vieira: 71 falhas = 46 reinícios certos + 25 possíveis mortes (janela: 4,3 obj/s, 164 px, 37 % sliders). Limites: HP sem a
  ordem dos erros (pior caso), sem breaks; janelas = densidade/espaçamento/velocidade/streams/sliders/"intensidade".
- **Registo de previsões + comparação automática** (`recommend/log.py`, tabelas `prediction_log`/`shadow_state`, `osuml eval-log [--since D --batch day|new] [--report-only]`, chamado no fim do `poll` quando há jogadores consultados; 0 pedidos):
  grava cada recomendação (com o modelo) e faz **avaliação-sombra** de QUALQUER mapa jogado (perfil só até ao início das jogadas novas); resultado por par = tentativas, passou, 1.ª tentativa, melhor accuracy, reinícios/mortes.
  Histórico desde 01/09 (3 843 pares, 73 jogadores; lazer n=1 786): P(passar) previsto 0,72 vs observado **0,68** (1.ª tentativa 0,62; previsto 0,75 → 0,68, 0,85 → 0,82, 0,94 → 0,89: ligeiramente optimista); **tentativas do lazer (3 065): 43 % passes,
  33 % reinícios certos, ≤ 23 % mortes possíveis** (por tentativa "não morrer" ≥ 77 %); accuracy ao passar viés **+1,6 pts**, MAE 4,2, **sem dependência da exigência** (1,5 / 1,7 / 1,8); viés por jogador varia de −0,6 a +5,1 pts (NBAH +5,1, LemonBread741 +4,8,
  Skinny_Ferny −0,6) ⇒ vale a pena uma **correção por jogador** (encolhimento `n/(n+20)`, já calculado no relatório, ainda não aplicada) e **recalibração periódica** em vez de re-treino constante (re-treino só com dumps novos).
  Pacote atual: `…0e69cefab468-passacc3.zip` (dados corrigidos; substitui `passacc2`).
  falhar — sai com P(passar) 81 %); melhor passe do par é ligeiramente optimista face a uma jogada avulsa. Pod `9s66ow1w5hs1si` (criado porque o `1v…` já não arranca por falta de memória no servidor) **parado**.
- **Índice v2** (`data/processed/recommend/index/`, antigo em `index_old25`; modelos antigos em `models_old25`): 152 268 mapas, matriz CF com 34 802 jogadores
  (50 %), 57 M pares, etiquetas 152 268 (130 184 com `set_id`). **Recomendador**: só prevê para candidatos plausíveis (`_predict_all(rows=)`) ⇒ **6 s** (era 29 s com o
  catálogo grande). PXD Vieira/speed: 493 candidatos novos; gaaGOD: 249 (speed +3,6…+4,7, outros eixos abaixo do nível).
- **Aplicação autónoma + pacote** (`recommend/{app,pack}.py`; `osuml recommend {serve,suggest,feedback,check,pack,upload}`): `serve` = página local (8770) — escrever o nome
  de um jogador que esteja na BD do pacote, escolher skills, "Serve / Não serve". **Links da dificuldade específica** (`beatmapsets/<set>#osu/<id>`; sem set: `/beatmaps/<id>`;
  verificado por HEAD: `/b/<id>` e `/beatmaps/<id>` redirecionam para aí) e **ID do mapa** visível. **Feedback também em texto**: `<pack>/feedback/recomendacoes_feedback.txt` (TSV com
  cabeçalho: data, jogador, user_id, beatmap_id, beatmapset_id, mapa, veredicto, tipo, skills, score, nota, link) + tabela `recommendation_feedback`. `check` mostra a
  impressão digital do modelo (novo `af8fe9c4933d`; antigo `1194cf78e401`) e o resumo do treino (`models/training.json`). **Distribuição** (decisão do utilizador): **código
  público no GitHub; pacote de dados à parte (privado)**, descompactado em `./pack` — dados derivados dos dumps (licença: só análise estatística) **não** vão numa release pública.
  Pacote `dist/osuml-pack.zip` (168 MB; só PXD Vieira + gaaGOD, colunas mínimas, sem JSON cru da API) **enviado ao bucket privado**
  `s3://osu-ml-skill/recommend/osuml-pack-0e69cefab468-passacc3.zip` (AES256, SHA-256 verificado; **esta é a versão a usar**; `passacc1` = perfil sem forma atual: modelos P(passar)+accuracy se passar; as anteriores `…af8fe9c4933d*.zip` são do modelo `reach`, obsoleto). Testado numa instalação limpa (só código + pacote, sem `data/` nem `.env`).
  Falta: o bloqueio de acesso público do bucket não se conseguiu verificar (IAM sem permissão) — confirmar na consola AWS.
- **Credenciais AWS estão no `.env`** (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`): o `boto3` só as vê depois de `Settings.from_env` carregar o `.env`; um teste feito
  fora disso deu falso negativo. Painel: a estimativa de tempo usa a **velocidade dos últimos 3 min** (a média desde o início enganava quando havia uma fase inicial sem passos).
- **Lições do pod**: `pkill -f`/`grep osuml` numa shell SSH cujo comando contém esse texto mata a própria shell (usar pids obtidos à parte); `uv` não está no PATH em ligações SSH
  sem `export PATH=$HOME/.local/bin:$PATH` (o reinstall falhou em silêncio); o extra `difficulty` (rosu-pp) tem de estar instalado no pod; `taskkill` ao PID do lançador não pára o
  servidor Python (matar o processo que escuta a porta). Pod `v1a4pwaky27sfk` **parado** (EXITED) — disco de 100 GB continua a cobrar armazenamento; o pod antigo
  `1u9bsup19laeo5` também parado. **Apagar ambos no RunPod se não forem reutilizados.**

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

- Correr `pytest` antes de dar uma alteração por concluída (196 testes, 1 só corre em Linux; todos devem passar).
  Testes nunca fazem pedidos reais: usar `httpx.MockTransport`.
- **Nunca apagar `data/raw/`**. Respostas raw são gravadas antes de normalizar.
- Datas guardadas em UTC *naive*.
- Não fazer commit de `.env` nem de `data/`.
- Não fazer pedidos reais à API durante desenvolvimento sem necessidade; preferir `status`
  (0 pedidos) para inspecionar o estado.
- Datasets versionados em `data/processed/<versão>/` com manifest (sha256, git commit, contagens).
