# Retreino mensal do recomendador (guia para quem executa — humano ou agente)

Corre-se **só** quando existe `data/control/retrain_pending.json` (criado pela tarefa `osuml-dumps-check` quando o data.ppy.sh publica um dump novo completo:
`random_10000` + `top_10000` + `osu_files` da mesma data). Sem esse ficheiro não há nada a fazer. O treino corre num pod RunPod (só CPU); tudo o resto é local.

## Regras (não negociáveis)
- **Nunca** pedidos à osu!API (o retreino usa só dumps e a BD local) e **nunca** credenciais no pod.
- Dumps do data.ppy.sh: uso privado (licença: só análise estatística). Nada disto vai para o GitHub nem para bucket público.
- **Custo**: pod `cpu5g` 16 vCPU 64 GB, **0,736 $/h**; um retreino demora ~2–3 h (~1,5–2,5 $). Diz o preço antes de criar. **Um pod só**; **máximo 5 h** de pod: se passar,
  para o pod e regista a falha. Não crias pods "para experimentar".
- No fim (com sucesso **ou** falha): **parar** o pod (`pod-action` `{"action":"stop"}`) depois de teres descarregado o que precisas — **sem perguntar**. **Nunca** `terminate`
  nem apagar dados; os pods parados antigos são do utilizador.
- **Sem `git push`, PR ou release.** Podes fazer commit local se alteraste código; o retreino normal não altera código.
- Uma falha **não se repete sozinha**: escreve `data/control/retrain_failed.json` (`{"snapshot":..., "reason":..., "at":...}`), pára o pod e termina com um resumo. Se esse ficheiro já existir para o
  mesmo `snapshot`, não voltes a tentar: só reporta.
- Bloqueio: antes de começar cria `data/control/retrain_running.json` (`{"snapshot", "started_at", "pod_id"}`); se já existir e `started_at` tiver < 6 h, não faças nada (há outra execução). Apaga-o no fim.
- Ambiente: Windows + Git Bash; Python em `.venv\Scripts\python.exe`. Usa `MSYS_NO_PATHCONV=1` nos comandos `ssh/scp` com caminhos `/root/...` (o Git Bash converte-os). Nunca `pkill -f`/`grep` com o
  próprio texto do comando numa shell SSH (mata a shell): obtém os pids à parte.

## Passos

### 1. Preparar (local, 0 custo)
```bash
cd "C:/Users/ricardo.vieira/osu!skill"
.venv/Scripts/python.exe -m osuml maintenance retrain-plan --prepare > data/control/retrain_plan.json
```
O JSON diz `snapshot`, `upload_to_pod_inputs` (Parquet antigos já tratados), `missing_local_files` (**tem de estar vazio**; se não estiver, falha e regista) e `pipeline_args`.
`--prepare` cria em `data/processed/maintenance/retrain/`: `osuml_src.zip`, `pod_pipeline.py`, `runpod_full_train.sh`, `inputs/api_plays.parquet`.

### 2. Criar o pod (custa dinheiro: 0,736 $/h)
`mcp__runpod__create-pod` com:
```json
{"name":"osuml-retrain","cpu":{"id":"cpu5g","vcpuCount":16},"image":"runpod/base:0.7.0-ubuntu2004","disk":100,"ports":["22/tcp","8888/http"],"cloud":"SECURE",
 "dataCenterIds":["EU-RO-1","EU-CZ-1","EUR-IS-1"],
 "env":{"PUBLIC_KEY":"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFSvTyJwb8u8nK0kew9lPEE8VlVhJmtCJyowgjck7hpd ricardoqv121@gmail.com"}}
```
Se falhar por falta de capacidade, tenta outros `dataCenterIds` (até 3 tentativas com uns minutos de intervalo); se continuar, regista a falha e termina (sem custo). Espera `RUNNING` (`get-pod`),
obtém IP e porta SSH direta (`ssh.direct`/portas públicas; se ainda for `null`, espera) e testa:
`ssh -i ~/.ssh/pod -p PORTA -o StrictHostKeyChecking=no root@IP "echo ok"` (a chave é `~/.ssh/pod`, sem passphrase; o proxy `ssh.runpod.io` **não** serve para `scp`).
Regista o `pod_id` em `retrain_running.json`.

### 3. Enviar
```bash
export MSYS_NO_PATHCONV=1
ssh -i ~/.ssh/pod -p PORTA root@IP "mkdir -p /root/work/inputs"
scp -i ~/.ssh/pod -P PORTA data/processed/maintenance/retrain/{osuml_src.zip,pod_pipeline.py,runpod_full_train.sh} root@IP:/root/work/
scp -i ~/.ssh/pod -P PORTA data/processed/maintenance/retrain/inputs/api_plays.parquet root@IP:/root/work/inputs/
scp -i ~/.ssh/pod -P PORTA <cada ficheiro de upload_to_pod_inputs> root@IP:/root/work/inputs/     # ~1,5 GB
```
Para o painel principal (8765) mostrar as barras de progresso, acrescenta a `data/control/remote_tasks.json` (lista JSON; não apagues entradas de outros):
`{"name":"pod-retreino","label":"Pod retreino","host_label":"RunPod cpu5g 16 vCPU","ssh":"root@IP","port":PORTA,"key":"~/.ssh/pod","progress_dir":"/root/work/progress","log":"/root/work/logs/pipeline.log"}`
(e remove essa entrada no fim).

### 4. Lançar o pipeline (corre em segundo plano no pod; sobrevive ao fim da ligação)
```bash
ssh -i ~/.ssh/pod -p PORTA root@IP "cd /root/work && export PATH=\$HOME/.local/bin:\$PATH && bash runpod_full_train.sh <pipeline_args do plano>"
```
(instala `uv`, Python 3.13 e `osuml[parquet,ml,difficulty]` — **o extra `difficulty` (rosu-pp) é obrigatório**; sem ele o catálogo "acaba" em segundos com tudo falhado, e o pipeline recusa se >50 % falharem).
Fases: 1 download+importação do dump novo (random ~1 GB, `top_10000` ~5,5 GB com 24 ligações, `osu_files` 1,4 GB) → 2 catálogo de mapas (~150 mil, 16 processos) → 3 resumo → 4 modelo P(passar) (sem `top_10000`)
→ 5 modelo da accuracy ao passar → 6 `retrain_outputs.tar`.

### 5. Acompanhar (sem poll agressivo: de 5 em 5 minutos, no máximo)
`ssh ... "tail -n 5 /root/work/logs/pipeline.log"` e `ssh ... "cat /root/work/progress/00_pipeline.json"`. Termina quando o log tiver `tudo pronto: retrain_outputs.tar`, ou `ERRO na fase` (então: regista a falha).
Se o pod passar de 5 h, pára-o e regista a falha (o que já estiver feito perde-se: não vale a pena gastar mais).

### 6. Descarregar (antes de parar o pod)
```bash
scp -i ~/.ssh/pod -P PORTA root@IP:/root/work/retrain_outputs.tar data/processed/maintenance/retrain/
scp -i ~/.ssh/pod -P PORTA root@IP:/root/work/osu_files.tar.bz2 data/processed/maintenance/retrain/    # 1,4 GB, para as etiquetas dos mapas
```
Confirma os tamanhos (`ls -l` local vs `ssh ... ls -l`) e **só então** `pod-action` `{"action":"stop"}` no pod (`get-pod` deve mostrar `EXITED`). Remove a entrada de `remote_tasks.json`.

### 7. Instalar (local; ~10–20 min)
```bash
cd data/processed/maintenance/retrain && mkdir -p outputs && tar -xf retrain_outputs.tar -C outputs && cd -
.venv/Scripts/python.exe -m osuml maintenance retrain-finish --outputs data/processed/maintenance/retrain/outputs --snapshot AAAA_MM_DD \
    --osu-files data/processed/maintenance/retrain/osu_files.tar.bz2
```
O `retrain-finish`: **recusa** o modelo novo se o AUC cair > 0,02 ou o MAE subir > 0,004 face ao atual (código de saída 2, `retrain_failed.json`; nada é alterado); senão reconstrói o índice em `index_new`,
recalibra P(passar) com os jogadores da API **depois** do dump (com poucos pares na 1.ª semana do mês mantém a calibração anterior e diz-o), troca as pastas (as antigas ficam como
`models_prev_<data>` / `index_prev_<data>`), refaz a avaliação-sombra dos últimos 90 dias com o modelo novo (para os ajustes por jogador), atualiza `maintenance_state.json` e apaga o pedido pendente.
Se o passo 7 recusar, **não é falha do pod**: o pod já está parado; regista o motivo no resumo.

### 8. Fechar
1. `.venv/Scripts/python.exe -m pytest -q` (têm de passar; se algum falhar por causa do modelo novo, reverte: renomeia `models_prev_*`/`index_prev_*` de volta e regista).
2. `.venv/Scripts/python.exe -m osuml maintenance pack-check --upload` (o pacote do S3 fica com o modelo novo; o índice/modelo mudaram, por isso é "pertinente").
3. Reinicia o painel 8765 para carregar o modelo novo (mata o processo que escuta na porta — pode haver mais de um; relança `python -m osuml panel --port 8765` em segundo plano).
4. Atualiza `CLAUDE.md` (secção do treino: snapshot, AUC, MAE, custo real do pod, tempo) e escreve `data/reports/manutencao/retreino_AAAA_MM_DD.txt` com: métricas antigas/novas, calibração (refeita ou mantida),
   pares da API usados, custo estimado (horas de pod × 0,736 $), chave do pacote no S3.
5. Apaga `retrain_running.json`. Resume ao utilizador em português de Portugal, em 5–8 linhas: o que mudou, custo, se o pod ficou parado (id), e o que ficou pendente.
