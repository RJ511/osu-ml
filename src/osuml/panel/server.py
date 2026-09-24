"""Servidor local do painel (`osuml panel`): só 127.0.0.1, biblioteca padrão, sem dependências.

Proteções: só aceita pedidos com `Host` local (contra DNS rebinding) e as ações (POST) exigem o
token aleatório embutido na página desta execução (contra outros sites a chamarem o localhost).
"""

from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .core import PanelController

PAGE = r"""<!doctype html>
<html lang="pt"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Painel de recolhas</title>
<style>
:root{--bg:#f5f2ea;--surface:#fff;--surface2:#ece6d6;--border:#ddd4bd;--text:#221f2b;--dim:#67617a;--accent:#a8386c;
--good:#2f8f57;--warn:#b9791f;--bad:#c14545;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#14131a;--surface:#1c1b25;--surface2:#26242f;--border:#383642;
--text:#eeecf5;--dim:#a29dbb;--accent:#e483ac;--good:#4fbf80;--warn:#dba13f;--bad:#e2726f;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,sans-serif;padding:16px}
.wrap{max-width:1200px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
h1{font-size:20px;margin:0}h2{font-size:12px;margin:0 0 8px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 14px}
.pill{font:600 12px ui-monospace,monospace;padding:4px 10px;border-radius:99px;background:var(--surface2)}
.pill.running{background:color-mix(in srgb,var(--good) 22%,transparent);color:var(--good)}
.pill.cancelling{background:color-mix(in srgb,var(--warn) 25%,transparent);color:var(--warn)}
button,select{font:inherit;padding:7px 12px;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--text)}
button.go{background:var(--accent);color:#fff;border-color:var(--accent)}button.stop{border-color:var(--bad);color:var(--bad)}
button:disabled{opacity:.45}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.tile b{display:block;font:600 24px ui-monospace,monospace;margin-top:2px}.tile span{font-size:12px;color:var(--dim)}
.tile.bad b{color:var(--bad)}.tile.good b{color:var(--good)}
.prog{height:10px;border-radius:99px;background:var(--surface2);overflow:hidden;display:flex}
.prog i{display:block;height:100%}.done{background:var(--good)}.cancelled{background:var(--warn)}.failed{background:var(--bad)}.running{background:var(--accent)}
.cols{display:grid;grid-template-columns:1fr 1.25fr;gap:14px}@media(max-width:900px){.cols{grid-template-columns:1fr}}
.scroll{overflow:auto;max-height:520px}table{border-collapse:collapse;width:100%;font-size:12.5px}
th{position:sticky;top:0;background:var(--surface2);text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--dim);padding:6px 8px}
td{padding:5px 8px;border-bottom:1px solid var(--border);white-space:nowrap}.mono{font-family:ui-monospace,monospace}
tr.viol td{background:color-mix(in srgb,var(--bad) 12%,transparent)}
.gapbar{display:inline-block;height:8px;border-radius:4px;background:var(--good);vertical-align:middle;margin-left:6px}
.gapbar.bad{background:var(--bad)}.dim{color:var(--dim)}.msg{color:var(--bad)}
.st-done{color:var(--good)}.st-failed{color:var(--bad)}.st-cancelled{color:var(--warn)}.st-running{color:var(--accent);font-weight:700}
</style></head><body><div class="wrap">
<div class="bar"><h1>Painel de recolhas</h1><span class="pill" id="status">—</span>
<span class="dim" id="msg"></span><span style="flex:1"></span>
<label class="dim">Intervalo mínimo <select id="interval"></select></label>
<button class="go" id="start">Iniciar</button><button class="stop" id="cancelAll">Cancelar tudo</button></div>

<div class="tiles">
<div class="card tile" id="tGap"><span>Último intervalo real (início→início)</span><b id="vGap">—</b></div>
<div class="card tile" id="tMin"><span>Mínimo medido / exigido</span><b id="vMin">—</b></div>
<div class="card tile" id="tViol"><span>Violações (abaixo do mínimo)</span><b id="vViol">0</b></div>
<div class="card tile" id="t60"><span>Pedidos nos últimos 60 s (máx. 60)</span><b id="v60">0</b></div>
<div class="card tile"><span>Próximo pedido possível em</span><b id="vNext">—</b></div>
</div>

<div class="card"><h2>Progresso <span id="pcount" class="dim"></span></h2><div class="prog" id="prog"></div></div>

<div class="cols">
<div class="card"><h2>Jogadores</h2><div class="scroll"><table><thead><tr><th>Jogador</th><th>Estado</th><th>Pedidos</th><th>Scores</th><th></th></tr></thead><tbody id="jobs"></tbody></table></div></div>
<div class="card"><h2>Pedidos enviados (mais recente primeiro)</h2><div class="scroll"><table><thead><tr><th>#</th><th>Hora</th><th>Pedido</th><th>HTTP</th><th>ms</th><th>Intervalo</th></tr></thead><tbody id="reqs"></tbody></table></div></div>
</div>
<div class="dim">Cancelar nunca deixa sair um pedido novo; um pedido já em voo termina. Cada jogador tem um orçamento máximo de pedidos. A recolha agendada de 12/12h é outro processo: enquanto uma corre, a outra espera (bloqueio comum).</div>
</div>
<script>
const TOKEN="__TOKEN__";
const $=id=>document.getElementById(id);
let S=null,skew=0;
const post=(p,b)=>fetch(p,{method:"POST",headers:{"Content-Type":"application/json","X-Panel-Token":TOKEN},body:JSON.stringify(b||{})}).then(r=>r.json());
[1.1,2,5,10,30,60].forEach(v=>{const o=document.createElement("option");o.value=v;o.textContent=v===1.1?"1,1 s (limite oficial)":v+" s";$("interval").appendChild(o)});
$("start").onclick=async()=>{const r=await post("/api/start",{min_interval:parseFloat($("interval").value)});if(r.error)$("msg").textContent=r.error;refresh()};
$("cancelAll").onclick=async()=>{await post("/api/cancel_all");refresh()};
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
function render(){
 if(!S)return;const running=S.status!=="idle";
 $("status").textContent=S.status==="idle"?"parado":S.status==="running"?"a recolher":"a cancelar…";$("status").className="pill "+S.status;
 $("msg").textContent=S.message||"";$("msg").className="msg";
 $("start").disabled=running;$("interval").disabled=running;$("cancelAll").disabled=!running&&!(S.counts.queued>0);
 if(running)$("interval").value=String(S.min_interval);
 const L=S.log,ev=L.events,last=ev[0];
 $("vGap").textContent=last&&last.gap_ms!==null?last.gap_ms+" ms":"—";
 $("tGap").className="card tile "+(last&&last.violation?"bad":"");
 $("vMin").textContent=(L.min_gap_ms===null?"—":L.min_gap_ms+" ms")+" / "+Math.round(S.min_interval*1000)+" ms";
 $("tMin").className="card tile "+(L.min_gap_ms!==null&&L.min_gap_ms<S.min_interval*1000-50?"bad":(L.total>1?"good":""));
 $("vViol").textContent=L.violations;$("tViol").className="card tile "+(L.violations?"bad":(L.total>1?"good":""));
 $("v60").textContent=L.last_60s;$("t60").className="card tile "+(L.last_60s>60?"bad":"");
 const c=S.counts,T=S.total_jobs||1;
 $("pcount").textContent=`— ${c.done||0} feitos · ${c.running||0} a correr · ${c.queued||0} em fila · ${c.cancelled||0} cancelados · ${c.failed||0} falhados de ${S.total_jobs}`;
 $("prog").replaceChildren(...["done","running","cancelled","failed"].map(k=>{const i=el("i",k);i.style.width=((c[k]||0)/T*100)+"%";return i}));
 const jb=$("jobs");jb.replaceChildren(...S.jobs.map(j=>{const tr=el("tr");tr.appendChild(el("td","",j.label+" (#"+j.user_id+")"));
  const st=el("td","st-"+j.status,j.status==="queued"?"em fila":j.status==="running"?"a correr":j.status==="done"?"feito":j.status==="cancelled"?"cancelado":"falhou");st.title=j.error||"";tr.appendChild(st);
  tr.appendChild(el("td","mono",j.requests));tr.appendChild(el("td","mono",j.scores_total??"—"));
  const td=el("td");if(j.status==="queued"||j.status==="running"){const b=el("button","stop","Cancelar");b.style.padding="2px 8px";b.onclick=async()=>{await post("/api/cancel_job",{user_id:j.user_id});refresh()};td.appendChild(b)}tr.appendChild(td);return tr}));
 $("reqs").replaceChildren(...ev.slice(0,60).map(e=>{const tr=el("tr",e.violation?"viol":"");
  tr.appendChild(el("td","mono dim",e.n));tr.appendChild(el("td","mono",new Date(e.start*1000).toLocaleTimeString()));
  tr.appendChild(el("td","mono",e.method+" "+e.path));tr.appendChild(el("td","mono",e.status??"erro"));tr.appendChild(el("td","mono",e.duration_ms));
  const g=el("td","mono",e.gap_ms===null?"—":e.gap_ms+" ms");if(e.gap_ms!==null){const b=el("span","gapbar"+(e.violation?" bad":""));b.style.width=Math.min(120,e.gap_ms/e.min_interval_ms*60)+"px";g.appendChild(b)}tr.appendChild(g);return tr}));
}
function tick(){if(!S)return;const L=S.log;if(L.since_last_start_ms===null||S.status==="idle"){$("vNext").textContent="—";return}
 const since=L.since_last_start_ms+(Date.now()/1000-skew-S.now)*1000;const rem=Math.max(0,S.min_interval*1000-since);$("vNext").textContent=rem<=0?"agora":Math.ceil(rem)+" ms"}
async function refresh(){try{S=await (await fetch("/api/state")).json();skew=Date.now()/1000-S.now;render()}catch(e){$("msg").textContent="sem ligação ao servidor"}}
refresh();setInterval(refresh,1000);setInterval(tick,100);
</script></body></html>"""


_NOT_FOUND = object()  # `None` é uma resposta válida de uma ação (ex.: cancelar); "não existe" é outra coisa


def _build_server(port: int, token: str, get_fn: Callable[[str], tuple[int, bytes, str] | None],
                  post_fn: Callable[[str, dict], Any]) -> ThreadingHTTPServer:
    """Servidor local: `Host` tem de ser local e as ações (POST) exigem o token da página."""
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # sem ruído no terminal
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            if self.headers.get("Host", "") not in allowed_hosts:
                return self._json({"error": "host inválido"}, 403)
            res = get_fn(self.path)
            if res is None:
                return self._json({"error": "não existe"}, 404)
            self._send(*res)

        def do_POST(self) -> None:
            if self.headers.get("Host", "") not in allowed_hosts or self.headers.get("X-Panel-Token") != token:
                return self._json({"error": "não autorizado"}, 403)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json({"error": "JSON inválido"}, 400)
            result = post_fn(self.path, body)
            if result is _NOT_FOUND:
                return self._json({"error": "não existe"}, 404)
            self._json(result if isinstance(result, dict) else {"ok": True})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def _json_bytes(obj: Any) -> tuple[int, bytes, str]:
    return 200, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8"


def make_generic_server(page: str, state_fn: Callable[[], Any], actions: dict[str, Callable[[dict], Any]],
                        port: int) -> tuple[ThreadingHTTPServer, str]:
    """Uma só página em `/` com a API em `/api/...`."""
    token = secrets.token_urlsafe(24)

    def get_fn(path: str):
        if path in ("/", "/index.html"):
            return 200, page.replace("__TOKEN__", token).encode(), "text/html; charset=utf-8"
        if path == "/api/state":
            return _json_bytes(state_fn())
        return None

    def post_fn(path: str, body: dict):
        action = actions.get(path)
        return _NOT_FOUND if action is None else action(body)

    return _build_server(port, token, get_fn, post_fn), token


def make_multi_server(shell: Callable[[list[str]], str],
                      sections: dict[str, tuple[str, Callable[[], Any], dict[str, Callable[[dict], Any]]]],
                      port: int) -> tuple[ThreadingHTTPServer, str]:
    """Várias secções (`/<nome>` = página, `/<nome>/api/...` = API) numa só porta, com um separador
    (`/`) que as junta. Cada página usa `"/api/..."`; aqui o prefixo `/<nome>` é acrescentado."""
    token = secrets.token_urlsafe(24)

    def get_fn(path: str):
        if path in ("/", "/index.html"):
            return 200, shell(list(sections)).encode(), "text/html; charset=utf-8"
        parts = path.strip("/").split("/", 1)
        if parts[0] in sections:
            page, state_fn, _ = sections[parts[0]]
            if len(parts) == 1:
                html = page.replace('"/api/', f'"/{parts[0]}/api/').replace("__TOKEN__", token)
                return 200, html.encode(), "text/html; charset=utf-8"
            if parts[1] == "api/state":
                return _json_bytes(state_fn())
        return None

    def post_fn(path: str, body: dict):
        parts = path.strip("/").split("/", 1)
        if parts[0] in sections and len(parts) == 2:
            action = sections[parts[0]][2].get("/" + parts[1])
            return _NOT_FOUND if action is None else action(body)
        return _NOT_FOUND

    return _build_server(port, token, get_fn, post_fn), token


def actions(controller: PanelController) -> dict[str, Callable[[dict], Any]]:
    def start(body: dict) -> dict:
        err = controller.start(body.get("min_interval"))
        return {"error": err} if err else {"ok": True}

    return {
        "/api/start": start,
        "/api/cancel_all": lambda body: controller.cancel_all(),
        "/api/cancel_job": lambda body: controller.cancel_job(int(body["user_id"])),
    }


def make_server(controller: PanelController, port: int) -> tuple[ThreadingHTTPServer, str]:
    return make_generic_server(PAGE, controller.state, actions(controller), port)
