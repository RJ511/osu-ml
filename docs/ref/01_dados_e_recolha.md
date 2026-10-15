# Dados e recolha

Fase 0 (collector), export, beatmaps e dificuldade, S3, observações sobre os dados reais, recolha agendada, jogadores acompanhados (painel multi-jogador, `poll`) e dumps oficiais.

> Secção movida do `CLAUDE.md` a 2026-10-15 (reorganização). O conteúdo é o original, por ordem cronológica de escrita; as decisões que não se revertem sem perguntar continuam resumidas no `CLAUDE.md`. Se algo aqui contradiz o `CLAUDE.md`, vale o `CLAUDE.md`.

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
