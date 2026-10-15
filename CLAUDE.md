# CLAUDE.md — osu! ML Skill & Recommendation System

Contexto para o Claude Code. Lê este ficheiro e `docs/design.md` antes de mexer no código; o histórico detalhado de cada tema está em `docs/ref/` (índice abaixo).
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

## Regras permanentes (resumo; o detalhe e o porquê estão em `docs/ref/`)

- **Pedidos externos**: osu!API ≤ 60/min (≥ 1,1 s entre pedidos), nunca pedidos novos sem autorização explícita (ver "Regra dura" abaixo). **Mirror hinai** só para o que o utilizador autorizou
  (tags, `.osu` **em falta** com MD5 validado, meta do top 100 PT, strains, rankings diários, disponibilidade de download): User-Agent `osuml/1.0 (+discord:.bieira)` (**nunca e-mail**),
  ≥ 1 s por pedido, guard consultado antes e a cada 25 pedidos (só a dor alta ≥ 4/5 pára; pressão com dor baixa = 1 pedido a cada 3 s), `Retry-After`, 3 falhas seguidas param, teto diário por
  finalidade (`mirror_usage.json`; tags 3 000 sets/dia). Detalhe: `docs/ref/07_mirror_tags_rankings.md`.
- **Dados de scores só pela osu!API oficial**; dumps do data.ppy.sh só para uso privado/estatístico (licença); mirrors de terceiros só com autorização.
- **Reiniciar não mede habilidade nem dificuldade** (perfeccionismo, bloqueio mental…): reinícios ficam fora do teste semanal, do nível, das previsões e da calibração; só as mortes são sinal de limite.
  Detalhe: `docs/ref/06_evolucao_treino_estilo.md`.
- **Aprendizagem**: nenhuma sugestão com accuracy esperada < 88 % (ideal ~93 %); P(passar) ≥ 80 % no nível "seguro".
- **Recomendações respeitam os limites medidos do jogador** (AR efetivo, tecto de estrelas por mods); escolher mods não é alavanca de pp.
- **Tailscale**: nunca `tailscale serve reset` (há outros serviços); filtrar o servidor 8765 por `-LocalAddress 127.0.0.1` ao matá-lo (o `tailscaled` também escuta nessa porta).
- **Windows**: o painel corre com `pythonw`; toda a chamada a `ssh`/`taskkill` leva `CREATE_NO_WINDOW`. `Path.write_text` traduz `\n` → `\r\n` (usar `write_bytes` onde o MD5 conta).
- **Operação**: credenciais AWS no `.env` (nunca imprimir valores); janelas de progresso no painel (separador Tarefas); mudar a definição de uma skill/escala ⇒ subir `SCHEME` e recategorizar tudo;
  cópias de segurança da BD (semanais e após cada retreino) vão para o S3 privado.
- **Respostas** em português de Portugal, diretas e imparciais; commits só quando o utilizador pede, `push` só quando pede.

### Regra dura: nunca pedir dados novos à API sem autorização explícita do utilizador
O utilizador pediu isto de forma direta: **nunca ultrapassar os limites/políticas da osu!API, e
pedir permissão antes de qualquer pedido novo** (isto cobre sobretudo expandir a recolha a outros
jogadores — a recolha já agendada de "PXD Vieira" de 12/12h já está autorizada e não precisa de
autorização repetida). Já era a prática seguida (perguntar antes de `maps fetch`, antes da pool de
referência, etc.) — fica agora registado como regra explícita, não só bom senso.

## Estado (2026-10-15)

Sistema ML-first de recomendação e acompanhamento do jogador **PXD Vieira** (objetivo: top 100 de Portugal), com página de Evolução (telemóvel/PC, Tailscale), teste semanal, sessões de treino com rondas,
rival dinâmico, projeção da meta, classes por estilo (tags), análise de falhas (morte vs reinício), recomendador com P(passar) + accuracy esperada calibrados por jogador, mods e ganho de pp, e
tarefas agendadas (recolha, `poll`, tags, manutenção mensal, retreino no RunPod, rankings diários, cópias de segurança). O que falta é sobretudo **tempo e dados**: jogar as sessões e votar nos mapas,
histórico de rankings (4-8 semanas), 1.º retreino real (novembro, a acompanhar), fase D das tags (1 351 sets, termina em ~1 dia). Pendentes e ideias: ver `docs/ref/` (cada ficheiro termina com o que ficou em aberto)
e a lista abaixo.

Em aberto (resumo): verificar na consola AWS o bloqueio de acesso público do bucket; ensaiar o retreino de ponta a ponta; usar o histórico da meta (`target_history`) na projeção; descobrir pares/rivais no ranking
de PT (pediria níveis à osu!API: precisa de autorização); sugerir troços difíceis por mapa com os strains; meia-vida da correção por jogador (decidir com 3-4 semanas de jogo regular).

## Documentação por tema (`docs/ref/`, histórico completo e números medidos)

| Ficheiro | Conteúdo |
|---|---|
| `docs/ref/01_dados_e_recolha.md` | Fase 0 (collector), export, beatmaps e dificuldade, S3, observações sobre os dados reais, recolha agendada, jogadores acompanhados (painel, `poll`), dumps oficiais |
| `docs/ref/02_skills_e_categorizacao.md` | Skills e nomenclatura, os 4 eixos, escala aberta, Reading (port do lazer), categorização, visualizador, painel Explorar |
| `docs/ref/03_catalogo_dumps_runpod.md` | Dumps de scores, catálogo de atributos por mapa×mods, RunPod e lições |
| `docs/ref/04_modelos_e_recomendador.md` | Validação do pp, baselines, modelo P(passar), recomendador v0/v1, calibração, correção por jogador, registo de previsões, pontos de falha, bloqueios, pacote autónomo |
| `docs/ref/05_manutencao_e_infra.md` | Painel de tarefas, manutenção e retreino, credenciais, carregamento mensal de mapas, Tailscale |
| `docs/ref/06_evolucao_treino_estilo.md` | Página de Evolução (meta, rival, teste, sessões, rondas, projeção), estilos por tags, falhas, reinícios |
| `docs/ref/07_mirror_tags_rankings.md` | Tags (fases A/B/C/D), regras de conduta, usos autorizados do mirror, rankings diários, cópias de segurança, disponibilidade |
| `docs/ref/08_pp_mods_e_ganho.md` | pp oficial, foco em ganho de pp, modo DT, seleção de mods e limites pessoais, registo por mods |
| `docs/design.md` | Documento de design original |
| `docs/skills_comunidade.md` | Discussão da nomenclatura de skills |
| `docs/retreino_mensal.md` | Guia passo a passo do retreino mensal no RunPod |

## Estrutura

```text
src/osuml/
  config.py               Settings a partir de .env
  cli.py / __main__.py    CLI: collect, poll, status, export, maps, categorize, panel, recommend, analyze, tags, maintenance, eval-log, sync-s3, dump-scores, map-catalog
  maintenance.py          manutenção agendada: dumps, retreino, pacote S3, rankings, cópias de segurança, osu-fill, goal-refresh, availability
  api/                    http.py (retry/backoff/logging seguro), rate_limit.py, auth.py, osu.py, lock.py
  collector/ scheduler/   collect de scores; tracker.py (poll contínuo: intervalos aleatórios, inativos, substitutos)
  storage/                models.py (esquema), database.py (Store), normalize.py, raw.py, s3.py
  dataset/ external/      export e colunas derivadas; leitores de dumps SQL/scores/tabelas do data.ppy.sh
  beatmaps/               acquire, parser, export, difficulty, reading, skills, hitfeatures, catalog, reference_pool
  categorize/ explore/    categorização de mapas/jogadores; painel Explorar (pesquisa, verificação por jogador)
  analysis/               modelos e validações (pass, acc, reach, pp, fail_points, calibração…)
  recommend/              core (Recommender), modsview, ppgain, adjust, log, index, pack, app
  evolution/              core, server (página), weekly (teste+sessões+rondas), styles, fails, projection, live, notify
  tags/                   collect, queue, monitor, server (tags do mirror hinai)
  mirror/                 client (MirrorClient: .osu, meta, strains, rankings, disponibilidade)
  panel/ jobs/            painel único (8765) e gestor de tarefas/progresso
tests/                    suite sem rede (API/S3/mirror simulados com httpx.MockTransport)
data/                     raw/ (nunca apagar), processed/<versão>/, control/, backups/, osuml.db  — fora do git
docs/                     design.md, skills_comunidade.md, retreino_mensal.md, ref/ (histórico por tema)
```

Tabelas principais: `runs`, `api_requests`, `users`, `scores` (PK `score_id`), `score_observations`, `beatmaps`, `beatmapsets`, `beatmap_files`, `collector_state`, `coverage_gaps`, `tracked_players`,
`map_categories`, `player_profiles`, `beatmap_tags`, `tag_fetches`, `tag_queue`, `tag_runs`, `prediction_log`, `recommendation_feedback`, `recommendation_blocks`.

## Convenções

- Correr `pytest` antes de dar uma alteração por concluída (325 testes, 1 só corre em Linux; todos devem passar).
  Testes nunca fazem pedidos reais: usar `httpx.MockTransport`.
- **Nunca apagar `data/raw/`**. Respostas raw são gravadas antes de normalizar.
- Datas guardadas em UTC *naive*.
- Não fazer commit de `.env` nem de `data/`.
- Não fazer pedidos reais à API durante desenvolvimento sem necessidade; preferir `status`
  (0 pedidos) para inspecionar o estado.
- Datasets versionados em `data/processed/<versão>/` com manifest (sha256, git commit, contagens).
- **Documentação**: o `CLAUDE.md` é só o resumo e o índice; ao acrescentar uma funcionalidade, regista os detalhes e os números medidos no ficheiro certo de `docs/ref/` (no fim), e só
  sobe ao `CLAUDE.md` uma regra permanente nova.
