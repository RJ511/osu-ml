"""Separador "Tags" do painel (`/tags`): estado dos sets e da API do mirror, mobile-first. Só mostra o que está na BD local;
o único pedido de rede é o botão "Verificar guard" (1 pedido, com intervalo mínimo)."""

from __future__ import annotations

from typing import Any, Callable

PAGE = r"""<!doctype html>
<html lang="pt"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Tags</title>
<style>
:root{--bg:#f5f2ea;--surface:#fff;--surface2:#ece6d6;--border:#ddd4bd;--text:#221f2b;--dim:#67617a;--accent:#a8386c;--good:#2f8f57;--warn:#b9791f;--bad:#c14545;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#14131a;--surface:#1c1b25;--surface2:#26242f;--border:#383642;--text:#eeecf5;--dim:#a29dbb;--accent:#e483ac;--good:#4fbf80;--warn:#dba13f;--bad:#e2726f;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 system-ui,sans-serif;padding:16px}
.wrap{max-width:1100px;margin:0 auto;display:grid;gap:12px;grid-template-columns:1fr}@media(min-width:900px){.wrap{grid-template-columns:1fr 1fr}.s2{grid-column:1/-1}}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:14px 16px;min-width:0}
h1{font-size:20px;margin:0}h2{font-size:12px;margin:0 0 10px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
.dim{color:var(--dim)}.mono{font-family:ui-monospace,monospace}.row{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}
.big{font:700 26px ui-monospace,monospace}.pill{font:700 13px ui-monospace,monospace;padding:4px 12px;border-radius:99px;color:#fff}
.green{background:var(--good)}.amber{background:var(--warn)}.red{background:var(--bad)}.unknown{background:var(--dim)}
.bar{height:10px;border-radius:99px;background:var(--surface2);overflow:hidden;margin:6px 0 2px}.bar i{display:block;height:100%;background:var(--accent)}
.note{font-size:12px;color:var(--dim);margin-top:6px}ul{margin:6px 0 0;padding-left:18px}
button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer}button.go{background:var(--accent);border-color:var(--accent);color:#fff}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:5px 6px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{font-size:11px;color:var(--dim);text-transform:uppercase}
.scroll{overflow:auto}.msg{font-size:13px;color:var(--bad)}
</style></head><body><div class="wrap" id="app"><div class="card s2 dim">A carregar…</div></div>
<script>
const TOKEN="__TOKEN__";const $=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined&&x!==null)e.textContent=x;return e};
const post=(p,b)=>fetch(p,{method:"POST",headers:{"Content-Type":"application/json","X-Panel-Token":TOKEN},body:JSON.stringify(b||{})}).then(r=>r.json());
const n0=v=>v==null?"—":Math.round(v).toLocaleString("pt-PT");
const LV={green:"VERDE · calmo",amber:"ÂMBAR · atenção",red:"VERMELHO · parado",unknown:"sem dados"};
const PH={B:"B · candidatos do recomendador",C:"C · jogadores parecidos",A:"A · amostra do catálogo"};
function ago(iso){if(!iso)return "nunca";const s=Math.max(0,(Date.now()-new Date(iso))/1000);if(s<90)return Math.round(s)+" s";if(s<5400)return Math.round(s/60)+" min";if(s<172800)return (s/3600).toFixed(1)+" h";return Math.round(s/86400)+" d"}
let msg="";
function render(s){
 const a=document.getElementById("app");a.replaceChildren();
 // estado geral + stress
 const h=$("div","card s2"),r=$("div","row");r.append($("h1","","Tags dos mapas — estado"),$("span","pill "+s.stress.level,LV[s.stress.level]));h.appendChild(r);
 const ul=$("ul");s.stress.reasons.forEach(x=>ul.appendChild($("li","",x)));h.appendChild(ul);
 if(s.paused)h.appendChild($("div","msg","Recolha PAUSADA (ficheiro TAGS_PAUSE): nenhuma execução agendada pede nada."));
 const bt=$("div","row");bt.style.marginTop="10px";
 const g=$("button","","Verificar guard agora (1 pedido)");g.onclick=async()=>{g.disabled=true;const x=await post("/tags/api/guard");msg=x.error||"";g.disabled=false;load()};
 const p=$("button",s.paused?"go":"",s.paused?"Retomar recolha":"Pausar recolha");p.onclick=async()=>{await post(s.paused?"/tags/api/resume":"/tags/api/pause");load()};
 bt.append(g,p);h.appendChild(bt);if(msg)h.appendChild($("div","msg",msg));a.appendChild(h);
 // sets
 const c=$("div","card");c.appendChild($("h2","","Sets"));
 const q=$("div","row");q.append($("span","big",n0(s.sets.ok)),$("span","dim",`recolhidos · ${n0(s.sets.not_found)} sem set · ${n0(s.sets.maps_with_tags)} mapas com tags`));c.appendChild(q);
 c.appendChild($("div","note",`Por fazer na fila: ${n0(s.sets.queue_todo)}${s.eta_days?` · ~${s.eta_days} dia(s) ao teto diário`:""}`));
 const bs=s.sets.by_source||{};c.appendChild($("div","note",`Fonte: endpoint local ${n0(bs.hinai_raw||0)} sets · /details ${n0(bs.hinai||0)} sets (os do /details têm votos mais frescos)`));
 Object.entries(s.queue).forEach(([k,v])=>{const w=$("div");w.style.marginTop="10px";const r2=$("div","row");r2.append($("span","",PH[k]||k),$("span","mono dim",`${n0(v.done)} / ${n0(v.total)}`));
  const b=$("div","bar"),i=$("i");i.style.width=(v.total?v.done/v.total*100:0)+"%";b.appendChild(i);w.append(r2,b);c.appendChild(w)});
 a.appendChild(c);
 // hoje
 const t=$("div","card");t.appendChild($("h2","","Teto diário (UTC)"));
 const tr=$("div","row");tr.append($("span","big",`${n0(s.today.used)}/${n0(s.today.cap)}`),$("span","dim",`restam ${n0(s.today.remaining)} sets hoje`));t.appendChild(tr);
 const b2=$("div","bar"),i2=$("i");i2.style.width=Math.min(100,s.today.used/s.today.cap*100)+"%";b2.appendChild(i2);t.appendChild(b2);
 t.appendChild($("div","note",`Respostas 429/503 nas últimas 24 h: ${s.throttles_24h}. Ritmo: 1 pedido/s; 2 s com dor ≥2/5; 3 s com pressão ativa e dor baixa; pára com dor ≥4/5.`));a.appendChild(t);
 // guard
 const gc=$("div","card s2");gc.appendChild($("h2","","API do mirror (guard)"));
 if(!s.guard)gc.appendChild($("div","dim","Ainda sem consulta."));
 else{const gr=$("div","row");gr.append($("span","pill "+s.guard.light,{green:"VERDE · 1 pedido/s",amber:"ÂMBAR · 1 pedido/2-3 s",red:"VERMELHO · parado (dor alta)"}[s.guard.light]),$("span","mono",s.guard.pressure?"pressão ativa":"sem pressão"),$("span","dim",`dor ${s.guard.pain_60s}/${s.guard.thresholds.pain_60s} nos últimos 60 s · verificado há ${ago(s.guard.at)}`));gc.appendChild(gr);
  gc.appendChild($("div","note",`Em vermelho (dor alta, ≥ 4/5) a recolha espera ${s.guard.thresholds.retry_after_s||120} s e volta a consultar; passados 30 min sem acalmar, termina sem pedir sets.`))}
 a.appendChild(gc);
 // execuções
 const rc=$("div","card s2");rc.appendChild($("h2","","Últimas execuções"));
 if(!s.runs.length)rc.appendChild($("div","dim","Nenhuma ainda."));
 else{const w=$("div","scroll"),tb=document.createElement("table"),hd=document.createElement("tr");["Início","Origem","Sets","Pedidos","Guard","ms méd","Notas"].forEach(x=>hd.appendChild($("th","",x)));tb.appendChild(hd);
  s.runs.forEach(x=>{const t2=document.createElement("tr"),sc=Object.entries(x.status_counts||{}).filter(([k])=>k!=="200").map(([k,v])=>k+"×"+v).join(" ");
   t2.append($("td","","há "+ago(x.started_at)),$("td","",x.trigger),$("td","mono",`${x.ok}${x.not_found?`+${x.not_found}`:""}/${x.planned}`),$("td","mono",x.requests),$("td","mono",x.guard_checks),$("td","mono",x.avg_ms?Math.round(x.avg_ms):"—"),$("td","",(x.aborted?x.aborted+" ":"")+sc+(x.note?x.note:"")));tb.appendChild(t2)});
  w.appendChild(tb);rc.appendChild(w)}
 a.appendChild(rc);
}
async function load(){try{render(await(await fetch("/tags/api/state")).json())}catch(e){document.getElementById("app").textContent="sem ligação ao servidor"}}
load();setInterval(load,5000);
</script></body></html>"""


def actions(monitor: Any) -> dict[str, Callable[[dict], Any]]:
    return {"/api/guard": monitor.guard_now, "/api/pause": monitor.pause, "/api/resume": monitor.resume}
