# Manutenção, infraestrutura e acesso remoto

Painel principal e tarefas, manutenção agendada e retreino, credenciais AWS, lições do pod, carregamento mensal de mapas e Tailscale.

> Secção movida do `CLAUDE.md` a 2026-10-15 (reorganização). O conteúdo é o original, por ordem cronológica de escrita; as decisões que não se revertem sem perguntar continuam resumidas no `CLAUDE.md`. Se algo aqui contradiz o `CLAUDE.md`, vale o `CLAUDE.md`.

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

- **Manutenção agendada (2026-09-25)** (`src/osuml/maintenance.py`, `osuml maintenance {status,monthly-sim,dumps-check,pack-check,retrain-plan [--prepare],retrain-finish}`; estado em `data/control/maintenance_state.json`, log em `data/logs/maintenance.jsonl`, 0 pedidos à osu!API):
  - **`osuml-manut-mensal`** (Task Scheduler, dia 1 às 03:20): `monthly-sim` simula a recalibração e escreve `data/reports/manutencao/AAAA-MM.{txt,json}`; nunca altera a calibração (aplicar é `recommend recalibrate --apply`).
  - **`osuml-dumps-check`** (diária 10:15; só age nos dias 1–14, alargado a 2026-10-15 porque o dump nem sempre sai no dia 1): procura na listagem do data.ppy.sh um dump **completo** (`random_10000` + `top_10000` + `osu_files` da mesma data) mais recente que `trained_snapshot` (hoje `2026_09_01`); se houver, escreve `data/control/retrain_pending.json`.
  - **`osuml-retreino`** (tarefa do Claude, `~/.claude/scheduled-tasks/osuml-retreino`, dias 2–14 às 11:30 (cron `30 11 2-14 * *`); corre com a app aberta, senão na próxima abertura): sem `retrain_pending.json` termina logo; com ele segue **`docs/retreino_mensal.md`** (pod RunPod cpu5g 16 vCPU 0,736 $/h, ~2–3 h ≈ 1,5–2,5 $, máx. 5 h; envia código+Parquet antigos, o pod descarrega só o dump novo; `scripts/pod_pipeline.py --random-snaps S --top-snap S --osu-files-snap S`;
    descarrega `retrain_outputs.tar`, **para** o pod, e `retrain-finish` instala). O `retrain-finish` **recusa** modelos piores (AUC −0,02 / MAE +0,004), reconstrói o índice e recalibra em pastas `*_new`, e só depois troca (as antigas ficam `*_prev_<data>`); recalibra com API só **depois** do dump (senão há fuga; com poucos pares mantém a calibração antiga) e refaz a avaliação-sombra de 90 dias com o modelo novo
    (os ajustes por jogador só usam previsões do modelo atual: `model_fp`). Uma falha não se repete sozinha (`retrain_failed.json`). **Ainda não foi exercitado de ponta a ponta num pod** (só os passos locais, com fakes, em `tests/test_maintenance.py`): o 1.º retreino real (dumps de 2026_10_01) deve ser acompanhado.
  - **`osuml-pack-check`** (semanal, domingo 05:10): reconstrói e envia o pacote para o S3 privado (`recommend/osuml-pack-<modelo>-<AAAAMMDD>.zip`) **só se for pertinente**: nunca enviado, ou modelo/calibração/índice mudaram, ou a correção de um jogador do pacote mudou ≥ 1 pt de accuracy ou ≥ 0,15 no logit de P(passar) (e há ≥ 7 dias do último envio). Chaves antigas ficam no bucket (limpar à mão). 1.º envio real feito: `recommend/osuml-pack-0e69cefab468-20260925.zip` (155 322 921 B; leva `player_adjust.json` dos 2 jogadores).
  - Pipeline do pod atualizado: passa a treinar também o **modelo da accuracy ao passar** (antes só o `reach`, obsoleto), não volta a descarregar dumps já tratados e devolve `retrain_outputs.tar`. `build_pack` já não rebenta com jogadores sem scores.
  - **Achado**: a tarefa `osuml-collect` (PXD Vieira de 12 em 12 h) **não existe** no agendador (só `osuml-poll`); o último pedido de scores do PXD foi hoje 07:57 UTC (provavelmente manual/painel).
- **Credenciais AWS estão no `.env`** (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`): o `boto3` só as vê depois de `Settings.from_env` carregar o `.env`; um teste feito
  fora disso deu falso negativo. Painel: a estimativa de tempo usa a **velocidade dos últimos 3 min** (a média desde o início enganava quando havia uma fase inicial sem passos).
- **Lições do pod**: `pkill -f`/`grep osuml` numa shell SSH cujo comando contém esse texto mata a própria shell (usar pids obtidos à parte); `uv` não está no PATH em ligações SSH
  sem `export PATH=$HOME/.local/bin:$PATH` (o reinstall falhou em silêncio); o extra `difficulty` (rosu-pp) tem de estar instalado no pod; `taskkill` ao PID do lançador não pára o
  servidor Python (matar o processo que escuta a porta). Pod `v1a4pwaky27sfk` **parado** (EXITED) — disco de 100 GB continua a cobrar armazenamento; o pod antigo
  `1u9bsup19laeo5` também parado. **Apagados no RunPod pelo utilizador (2026-10-15): não há pods; `remote_tasks.json` está `[]`.**

- **Carregamento mensal dos mapas automático** (`osuml maintenance maps-load`, `maintenance.maps_load`; 0 pedidos à osu!API): tarefa **`osuml-maps-load`** (diária 10:40, termina logo se não há dump novo;
  janela = o mês todo porque o dump nem sempre sai no dia 1, ex. 2026_07_13). Se há `*_osu_files.tar.bz2` mais recente que `maps_loaded_snapshot` **e** mapas referenciados sem ficheiro: descarrega para
  `data/external/osu_files/` (streaming, `.part`, confere tamanho e espaço livre ≥ 2,5×), importa por MD5, recategoriza (o `no_file` com ficheiro novo é recalculado) e só então grava o estado; mantém só o
  dump mais recente; se falhar repete no dia seguinte. Fica de fora o que ainda não estava no dump (ranked/loved depois do dia do dump → só no seguinte). **Em aberto**: `dumps-check` (retreino) continua
  com janela dos dias 1–14 (alargada a 2026-10-15; `maintenance.WINDOW_DAYS`) e a tarefa `osuml-retreino` nos dias 2–14 — um dump que saia depois do dia 14 não dispara o retreino sozinho.
- **Acesso remoto**: `tailscale serve --bg --https=8765 http://127.0.0.1:8765` (só acrescenta a porta 8765; **nunca usar `tailscale serve reset`**: a
  tailnet tem outros serviços em 443/8001/8080/8443). Endereço: `https://alu-m.tail424d06.ts.net:8765` (HTTPS e **pelo nome**, não pelo IP). O servidor
  continua em 127.0.0.1; `host_allowed` (`panel/server.py`) só aceita o `Host` da tailnet se o pedido vier do proxy local **e** o cabeçalho
  `Tailscale-User-Login` for o utilizador de `OSUML_REMOTE_USER` (a tailnet tem dispositivos de outras contas). `OSUML_REMOTE_HOST/USER` no `.env`.
  Desligar: `tailscale serve --https=8765 off`. Cuidado: `Get-NetTCPConnection -LocalPort 8765` também lista o `tailscaled`; filtrar por `-LocalAddress 127.0.0.1`.
