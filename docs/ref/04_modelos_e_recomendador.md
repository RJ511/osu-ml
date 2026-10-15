# Modelos e recomendador

Validação do pp, baselines de accuracy, modelo P(passar), tentativas do dump, recomendador v0/v1, calibração, correção por jogador, registo de previsões, pontos de falha, bloqueios e pacote autónomo.

> Secção movida do `CLAUDE.md` a 2026-10-15 (reorganização). O conteúdo é o original, por ordem cronológica de escrita; as decisões que não se revertem sem perguntar continuam resumidas no `CLAUDE.md`. Se algo aqui contradiz o `CLAUDE.md`, vale o `CLAUDE.md`.

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
  Skinny_Ferny −0,6) ⇒ vale a pena uma **correção por jogador** (**já aplicada**, ver abaixo) e **recalibração periódica** em vez de re-treino constante (re-treino só com dumps novos).
- **Correção por jogador + recalibração (2026-09-25)** (`recommend/adjust.py`; `osuml recommend recalibrate [--apply]`): `models/player_adjust.json` (por jogador: `acc_bias` encolhido `Σd/(n+10)`, `pass_offset` no logit `Σ(y−p)/(Σp(1−p)+1/τ²)`, τ=0,5; só com
  ≥ 5 / ≥ 10 pares; estima-se **só** com as avaliações-sombra, a partir dos valores BRUTOS + a calibração global atual, para não ser circular; o registo das recomendações grava os valores do MODELO, antes da correção). Aplica-se em `Recommender.recommend` (relê o ficheiro se mudou:
  o `poll` é outro processo e atualiza-o no fim de cada avaliação, e o `eval-log` também). **Validação fora do tempo** (cada previsão só com o passado do jogador; 3 843 pares): MAE da accuracy 4,37 → 4,07 pts (K=10; 4,01 se encolher para o viés global), Brier de P(passar) 0,195 → 0,183
  (τ=0,5). Efeito real: PXD Vieira acc −1,8 pts e P(passar) −0,49 no logit ⇒ a lista "segura" muda por completo (0/20 em comum); gaaGOD −0,2 pts / −0,16 (11/20 em comum). O pacote leva o `player_adjust.json` só dos jogadores do pacote (**o pacote S3 `passacc3` ainda não o tem: refazer e
  reenviar quando se quiser**). **`recalibrate`** refaz a calibração global (Platt + deslocamento da accuracy) com o registo (só lazer; ≥ 500 pares e ≥ 30 jogadores) e só grava com `--apply` **e** se a validação cruzada por jogador melhorar (Brier ≥ 0,002 / MAE ≥ 0,0005; guarda `.bak-<data>`).
  Hoje (1 786 pares lazer, 48 jogadores) NÃO compensa: Brier 0,1815 → 0,1801 (ECE 0,058 → 0,028) ⇒ nada foi alterado. Cadência sugerida: correr o `recalibrate` (simulação) ~1×/mês; re-treinar o modelo só com dumps novos.
- **Mapas bloqueados** (`recommendation_blocks`; `Recommender.block_map/unblock/blocks`, `parse_map_ref`): preferência do jogador de NÃO receber um mapa — `set` (omissão: o mapa inteiro, todas as dificuldades, via `set_id` do índice) ou `diff` (só essa dificuldade; também quando o set é desconhecido, ~21 mil mapas sem `set_id`). Por jogador, sem duplicados,
  aplicado na elegibilidade (também conta em `player.n_blocked`), independente do "Não serve" (que só regista). Cada (des)bloqueio acrescenta uma linha ao TSV de feedback (`bloquear_mapa`/`bloquear_dificuldade`/`desbloquear`). UI: botões "Não recomendar o mapa" / "só esta dificuldade" em cada sugestão + campo para colar link/ID + lista com "Desbloquear",
  na app autónoma (8770) e no Explorar (8765) (`/api/block|unblock|blocks`, `/api/rec_block|rec_unblock|rec_blocks`). Aceita links `beatmapsets/<set>#osu/<id>`, `/beatmaps/<id>`, `/b/<id>` e IDs soltos (ID solto = dificuldade).
- **Explorar → mapas do catálogo** (2026-09-25): a lista de mapas só mostrava os **8 493** mapas com plays dos 75 jogadores da API (a BD só guarda metadados desses). O Explorar usa agora também o índice do recomendador (`Recommender.catalog()`, sem carregar modelos): **152 721 mapas** (152 268 do catálogo + 453 só da BD); pesquisa por texto/id de mapa/id de set
  (os que têm plays acompanhados vêm primeiro, `catalog_only` para os restantes) e ficha com atributos reais por mods do `catalog/v2/map_attributes.parquet` (notas 0-100 só sem mods; sem plays). ~0,8 s por pesquisa.
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
