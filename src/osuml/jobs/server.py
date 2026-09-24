"""Separador "Tarefas" do painel (`/tasks`): barras de progresso de todas as tarefas longas + botões para lançar/cancelar."""

from __future__ import annotations

from typing import Any, Callable

from .core import JobManager

PAGE = r"""<!doctype html>
<html lang="pt"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tarefas</title>
<style>
:root{--bg:#f5f2ea;--surface:#fff;--surface2:#ece6d6;--border:#ddd4bd;--text:#221f2b;--dim:#67617a;--accent:#a8386c;--good:#2f8f57;--warn:#b9791f;--bad:#c14545;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#14131a;--surface:#1c1b25;--surface2:#26242f;--border:#383642;--text:#eeecf5;--dim:#a29dbb;--accent:#e483ac;--good:#4fbf80;--warn:#dba13f;--bad:#e2726f;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,sans-serif;padding:16px}
.wrap{max-width:1100px;margin:0 auto;display:flex;flex-direction:column;gap:14px}h1{font-size:20px;margin:0}h2{font-size:12px;margin:0 0 8px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 14px}.sub{color:var(--dim);font-size:12.5px}.mono{font-family:ui-monospace,monospace}
button{font:inherit;padding:7px 12px;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer}
button.go{background:var(--accent);border-color:var(--accent);color:#fff}button.stop{border-color:var(--bad);color:var(--bad)}button:disabled{opacity:.45;cursor:not-allowed}
.tpls{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}.tpl{border:1px solid var(--border);border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:6px}
.tpl b{font-size:13.5px}.task{display:flex;flex-direction:column;gap:6px}.head{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.head b{font-size:14.5px}.head .sp{flex:1}
.pill{font:600 11.5px ui-monospace,monospace;padding:3px 10px;border-radius:99px;background:var(--surface2)}
.pill.running{color:var(--accent)}.pill.done{color:var(--good)}.pill.error,.pill.interrupted{color:var(--bad)}
.bar{height:16px;border-radius:99px;background:var(--surface2);overflow:hidden;border:1px solid var(--border)}.bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .6s ease}
.bar.done i{background:var(--good)}.bar.error i,.bar.interrupted i{background:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px}.grid span{font-size:11.5px;color:var(--dim);display:block}.grid b{font:600 14px ui-monospace,monospace}
pre{background:var(--surface2);border-radius:8px;padding:8px;font-size:11.5px;overflow:auto;max-height:110px;margin:0;white-space:pre-wrap}.empty{color:var(--dim);padding:14px;text-align:center}.msg{color:var(--bad);font-size:12.5px}
</style></head><body><div class="wrap">
<div><h1>Tarefas e progresso</h1><div class="sub">Todas as tarefas longas do projeto (lançadas aqui ou num terminal). Só lê os ficheiros de progresso e lança modelos pré-definidos — nunca comandos arbitrários; nenhuma faz pedidos à osu!API.</div></div>
<div class="card"><h2>Lançar</h2><div class="tpls" id="tpls"></div><div class="msg" id="msg"></div></div>
<div id="tasks" style="display:flex;flex-direction:column;gap:10px"></div>
</div>
<script>
const TOKEN="__TOKEN__",$=id=>document.getElementById(id);
const post=(p,b)=>fetch(p,{method:"POST",headers:{"Content-Type":"application/json","X-Panel-Token":TOKEN},body:JSON.stringify(b||{})}).then(r=>r.json());
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined&&x!==null)e.textContent=x;return e};
const dur=s=>{if(s==null||!isFinite(s)||s<0)return"—";s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return h?`${h} h ${m} min`:m?`${m} min ${x} s`:`${x} s`};
const n0=v=>Number(v).toLocaleString("pt-PT",{maximumFractionDigits:0});
const unit=(u,v)=>u==="bytes"?(v/1048576).toLocaleString("pt-PT",{maximumFractionDigits:0})+" MB":n0(v)+(u?" "+u:"");
const PILL={running:"em curso",done:"concluída",error:"erro / cancelada",interrupted:"interrompida / sem ligação"};
async function refresh(){
 let s;try{s=await(await fetch("/api/state")).json()}catch(e){$("msg").textContent="sem ligação ao servidor";return}
 $("tpls").replaceChildren(...s.templates.map(t=>{const d=el("div","tpl");d.appendChild(el("b","",t.label));d.appendChild(el("div","sub",t.description+(t.eta?" · "+t.eta:"")));
  const b=el("button","go",t.running?"Em curso…":"Lançar");b.disabled=t.running||t.missing.length>0||t.blocked_by.length>0;if(t.missing.length)b.title="falta "+t.missing[0];
  b.onclick=async()=>{b.disabled=true;const r=await post("/api/start",{name:t.name});$("msg").textContent=r.error||"";refresh()};d.appendChild(b);
  if(t.missing.length)d.appendChild(el("div","sub","falta: "+t.missing[0]));
  if(t.blocked_by.length)d.appendChild(el("div","sub","à espera de outras tarefas em curso"));return d}));
 if(!s.tasks.length){$("tasks").replaceChildren(el("div","card empty","Sem tarefas recentes. Lança uma acima."));return}
 $("tasks").replaceChildren(...s.tasks.map(t=>{const c=el("div","card task"),h=el("div","head");h.appendChild(el("b","",t.label));
  if(t.remote){const rb=el("span","pill","☁ "+t.remote);rb.title="a correr noutra máquina (pod)";h.appendChild(rb)}
  h.appendChild(el("span","pill "+t.status,PILL[t.status]||t.status));h.appendChild(el("span","sp"));
  if(t.status==="running"&&t.alive&&!t.unreachable){const b=el("button","stop","Cancelar");b.onclick=async()=>{if(!confirm("Cancelar esta tarefa?"))return;b.disabled=true;const r=await post("/api/cancel",{key:t.key});$("msg").textContent=r.error||"";refresh()};h.appendChild(b)}
  c.appendChild(h);const bar=el("div","bar "+(t.status==="running"?"":t.status)),i=el("i");i.style.width=t.pct+"%";bar.appendChild(i);c.appendChild(bar);
  const g=el("div","grid");const cell=(l,v)=>{const d=el("div");d.appendChild(el("span","",l));d.appendChild(el("b","",v));g.appendChild(d)};
  cell("Progresso",t.pct.toFixed(1)+" %");cell("Feito",unit(t.unit,t.done)+" / "+unit(t.unit,t.total));
  cell("Velocidade",t.rate?(t.unit==="bytes"?(t.rate/1048576).toFixed(1)+" MB/s":t.rate.toFixed(1)+" "+t.unit+"/s"):"—");
  cell("Decorrido",dur(t.elapsed_s));cell("Restante (est.)",t.status==="running"?dur(t.eta_s):"—");
  cell(t.status==="running"?"Último sinal":"Terminou",t.status==="running"?"há "+dur(t.age_s):"há "+dur(t.age_s));c.appendChild(g);
  if(t.log){const p=el("pre");p.textContent=t.log;c.appendChild(p)}
  const f=el("div","sub mono","ficheiro: "+t.key+(t.pid?" · pid "+t.pid:"")+(t.exit_code!=null?" · código de saída "+t.exit_code:""));c.appendChild(f);return c}));
}
refresh();setInterval(refresh,2000);
</script></body></html>"""


def actions(jobs: JobManager) -> dict[str, Callable[[dict], Any]]:
    return {
        "/api/start": lambda body: jobs.start(str(body.get("name") or "")),
        "/api/cancel": lambda body: jobs.cancel(str(body.get("key") or "")),
    }
