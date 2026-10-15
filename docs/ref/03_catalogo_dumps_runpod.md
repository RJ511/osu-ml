# Catálogo de mapas, dumps de scores e RunPod

Importação dos dumps de scores, catálogo de atributos por mapa×mods, uso do RunPod e as lições aprendidas no pod.

> Secção movida do `CLAUDE.md` a 2026-10-15 (reorganização). O conteúdo é o original, por ordem cronológica de escrita; as decisões que não se revertem sem perguntar continuam resumidas no `CLAUDE.md`. Se algo aqui contradiz o `CLAUDE.md`, vale o `CLAUDE.md`.

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
