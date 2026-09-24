"""Painel local da categorização (`osuml categorize`): mostra, em direto, que jogador (nome, pp, rank)
e que mapa estão a ser categorizados, e o resultado. Mesmas proteções do painel de recolhas."""

from __future__ import annotations

from http.server import ThreadingHTTPServer
from typing import Any, Callable

from ..panel.server import make_generic_server
from .core import CategorizeController

PAGE = r"""<!doctype html>
<html lang="pt"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Categorização</title>
<style>
:root{--bg:#f5f2ea;--surface:#fff;--surface2:#ece6d6;--border:#ddd4bd;--text:#221f2b;--dim:#67617a;--accent:#a8386c;
--good:#2f8f57;--warn:#b9791f;--bad:#c14545;--aim:#6f5fc9;--speed:#1f8f86;--stamina:#c9622f;--reading:#2f74b8;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#14131a;--surface:#1c1b25;--surface2:#26242f;--border:#383642;
--text:#eeecf5;--dim:#a29dbb;--accent:#e483ac;--good:#4fbf80;--warn:#dba13f;--bad:#e2726f;--aim:#a597ee;--speed:#4fc2b7;
--stamina:#f0925e;--reading:#5fa4ea;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,sans-serif;padding:16px}
.wrap{max-width:1280px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
h1{font-size:20px;margin:0}h2{font-size:12px;margin:0 0 8px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 14px}
.pill{font:600 12px ui-monospace,monospace;padding:4px 10px;border-radius:99px;background:var(--surface2)}
.pill.running{background:color-mix(in srgb,var(--good) 22%,transparent);color:var(--good)}.pill.paused,.pill.cancelling{background:color-mix(in srgb,var(--warn) 25%,transparent);color:var(--warn)}
.pill.done{background:color-mix(in srgb,var(--accent) 22%,transparent);color:var(--accent)}
button,select{font:inherit;padding:7px 12px;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--text)}
button.go{background:var(--accent);color:#fff;border-color:var(--accent)}button.stop{border-color:var(--bad);color:var(--bad)}button:disabled{opacity:.45}
.now{display:grid;grid-template-columns:1fr 1.6fr;gap:14px}@media(max-width:900px){.now{grid-template-columns:1fr}}
.big{font:700 22px system-ui;margin:2px 0}.sub{color:var(--dim);font-size:12.5px}.mono{font-family:ui-monospace,monospace}
.chip{display:inline-block;font:600 11px ui-monospace,monospace;padding:2px 8px;border-radius:99px;background:var(--surface2);margin-right:4px}
.chip.new{background:color-mix(in srgb,var(--good) 22%,transparent);color:var(--good)}.chip.cached{color:var(--dim)}
.chip.no_file,.chip.error{background:color-mix(in srgb,var(--bad) 20%,transparent);color:var(--bad)}
.axes{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:10px}
.ax{border:1px solid var(--border);border-radius:10px;padding:8px;border-top:3px solid var(--c)}.ax b{font:700 20px ui-monospace,monospace}
.ax .lbl{font-size:11px;text-transform:uppercase;color:var(--dim)}.trk{height:5px;border-radius:9px;background:var(--surface2);margin-top:4px;overflow:hidden}
.trk i{display:block;height:100%;background:var(--c)}
.prog{height:10px;border-radius:99px;background:var(--surface2);overflow:hidden;margin:4px 0 8px}.prog i{display:block;height:100%;background:var(--accent)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.tile span{font-size:11.5px;color:var(--dim)}.tile b{display:block;font:600 20px ui-monospace,monospace}
.cols{display:grid;grid-template-columns:1.25fr 1fr;gap:14px}@media(max-width:1000px){.cols{grid-template-columns:1fr}}
.scroll{overflow:auto;max-height:560px}table{border-collapse:collapse;width:100%;font-size:12.5px}
th{position:sticky;top:0;background:var(--surface2);text-align:left;font-size:11px;text-transform:uppercase;color:var(--dim);padding:6px 8px}
td{padding:5px 8px;border-bottom:1px solid var(--border);white-space:nowrap}tr.cur td{background:color-mix(in srgb,var(--accent) 12%,transparent)}
.g{font:700 11px ui-monospace,monospace;padding:1px 6px;border-radius:5px;margin-right:3px;background:var(--surface2)}
.g.hi{background:#e6b422;color:#221f2b}.g.a{background:color-mix(in srgb,var(--good) 30%,transparent)}.g.b{background:color-mix(in srgb,var(--reading) 28%,transparent)}
.dim{color:var(--dim)}.msg{color:var(--bad)}.faded{opacity:.55}
</style></head><body><div class="wrap">
<div class="bar"><h1>Categorização de jogadores e mapas</h1><span class="pill" id="status">—</span><span class="msg" id="msg"></span><span style="flex:1"></span>
<label class="dim">Ritmo <select id="delay"><option value="0">máximo</option><option value="20">20 ms / mapa novo</option><option value="100">100 ms / mapa novo</option><option value="500">500 ms / mapa novo</option><option value="1500">1,5 s / mapa novo</option></select></label>
<button class="go" id="start">Iniciar</button><button id="pause">Pausar</button><button class="stop" id="cancel">Cancelar</button></div>

<div class="now">
<div class="card"><h2>Jogador agora <span class="dim" id="pidx"></span></h2><div class="big" id="pname">—</div><div class="sub" id="ppp">—</div>
<div class="prog"><i id="pbar" style="width:0"></i></div><div class="sub" id="pmaps">—</div></div>
<div class="card"><h2>Mapa a ser categorizado <span id="mchip"></span></h2><div class="big" id="mname">—</div><div class="sub" id="minfo">—</div>
<div class="axes" id="axes"></div></div>
</div>

<div class="card"><h2>Progresso</h2>
<div class="sub" id="l1"></div><div class="prog"><i id="b1" style="width:0"></i></div>
<div class="sub" id="l2"></div><div class="prog"><i id="b2" style="width:0"></i></div>
<div class="tiles"><div class="tile"><span>Categorizados agora</span><b id="tNew">0</b></div><div class="tile"><span>Reaproveitados (cache)</span><b id="tCached">0</b></div>
<div class="tile"><span>Sem .osu</span><b id="tNoFile">0</b></div><div class="tile"><span>Erros</span><b id="tErr">0</b></div>
<div class="tile"><span>Ritmo</span><b id="tRate">—</b></div><div class="tile"><span>Tempo</span><b id="tTime">—</b></div></div></div>

<div class="cols">
<div class="card"><h2>Jogadores (por pp)</h2><div class="scroll"><table><thead><tr><th>#</th><th>Jogador</th><th>pp</th><th>Rank</th><th>Mapas</th><th>Estado</th><th>Aim</th><th>Speed</th><th>Stam.</th><th>Read.</th></tr></thead><tbody id="players"></tbody></table></div>
<div class="sub" id="rule" style="margin-top:6px"></div></div>
<div class="card"><h2>Atribuições (mais recente primeiro)</h2><div class="scroll"><table><tbody id="feed"></tbody></table></div></div>
</div>
<div class="card"><h2>Validação: os ratings acompanham o pp? (Spearman, jogadores com ≥10 plays de evidência)</h2><div class="sub" id="corr">—</div></div>
</div>
<script>
const TOKEN="__TOKEN__",$=id=>document.getElementById(id);let S=null;
const post=(p,b)=>fetch(p,{method:"POST",headers:{"Content-Type":"application/json","X-Panel-Token":TOKEN},body:JSON.stringify(b||{})}).then(r=>r.json());
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined&&x!==null)e.textContent=x;return e};
const AX=[["aim","Aim","--aim"],["speed","Speed","--speed"],["stamina","Stamina","--stamina"],["reading","Reading","--reading"]];
const gc=g=>["X","SSS","SS","S+","S"].includes(g)?"hi":g==="A"?"a":(g==="B+"||g==="B")?"b":"";
const fmt=n=>n==null?"—":Number(n).toLocaleString("pt-PT",{maximumFractionDigits:0});
const mm=s=>Math.floor(s/60)+":"+String(s%60).padStart(2,"0");
$("start").onclick=async()=>{const r=await post("/api/start",{delay_ms:+$("delay").value});if(r.error)$("msg").textContent=r.error;refresh()};
$("pause").onclick=async()=>{await post(S&&S.status==="paused"?"/api/resume":"/api/pause");refresh()};
$("cancel").onclick=async()=>{await post("/api/cancel");refresh()};
$("delay").onchange=()=>post("/api/speed",{delay_ms:+$("delay").value});
function grades(prof){const f=document.createDocumentFragment();AX.forEach(([k])=>{const g=prof&&prof.ratings[k+"_grade"];const s=el("span","g "+gc(g),g||"–");if(prof&&!prof.ratings.confident)s.style.opacity=".5";f.appendChild(s)});return f}
function render(){
 if(!S)return;const run=S.status==="running"||S.status==="paused"||S.status==="cancelling";
 $("status").textContent={idle:"parado",running:"a categorizar",paused:"em pausa",cancelling:"a cancelar…",done:"concluído"}[S.status];$("status").className="pill "+S.status;
 $("msg").textContent=S.message||"";$("start").disabled=run;$("pause").disabled=!(S.status==="running"||S.status==="paused");$("pause").textContent=S.status==="paused"?"Retomar":"Pausar";$("cancel").disabled=!run;
 if(run)$("delay").value=String(S.delay_ms);
 const e=S.current;
 if(e){$("pidx").textContent=`(${e.player_idx}/${e.n_players})`;$("pname").textContent=(e.username||("jogador #"+e.user_id));
  $("ppp").textContent=`${e.pp!=null?fmt(e.pp)+" pp":"pp indisponível"} · ${e.global_rank!=null?"rank global #"+fmt(e.global_rank):"rank indisponível"} · id ${e.user_id}`;
  $("pbar").style.width=(e.map_idx/e.n_maps*100)+"%";$("pmaps").textContent=`mapa ${e.map_idx} de ${e.n_maps} deste jogador`;
  $("mname").textContent=`${e.artist?e.artist+" – ":""}${e.title||("mapa #"+e.beatmap_id)} [${e.version||"?"}]${e.mods?" +"+e.mods.replaceAll(","," "):""}`;
  const r=e.raw||{};$("minfo").textContent=e.status==="ok"?`#${e.beatmap_id} · ${(r.stars||0).toFixed(2)}★ · AR ${r.ar} · OD ${r.od} · CS ${r.cs} · HP ${r.hp} · ${r.n_objects} objetos`:`#${e.beatmap_id} · ${e.error||""}`;
  const ch=el("span","chip "+(e.status!=="ok"?e.status:e.cached?"cached":"new"),e.status==="no_file"?"sem .osu":e.status==="error"?"erro":e.cached?"já categorizado (cache)":"nova categoria");$("mchip").replaceChildren(ch);
  $("axes").replaceChildren(...AX.map(([k,l,c])=>{const sc=e.scores&&e.scores[k+"_score"];const d=el("div","ax");d.style.setProperty("--c","var("+c+")");
   d.appendChild(el("div","lbl",l));d.appendChild(el("b","",sc!=null?e.scores[k+"_grade"]:"–"));d.appendChild(el("div","sub mono",sc!=null?sc.toFixed(1)+" · top "+(100-e.scores[k+"_pct"]).toFixed(1)+"%":""));
   const t=el("div","trk");const i=el("i");i.style.width=(Math.min(120,Math.max(0,sc||0))/1.2)+"%";t.appendChild(i);d.appendChild(t);return d}))}
 const c=S.counts,saved=Object.values(S.pairs_saved).reduce((a,b)=>a+b,0);
 $("l1").textContent=`Jogadores: ${S.players_done} de ${S.players_total}`;$("b1").style.width=(S.players_done/Math.max(1,S.players_total)*100)+"%";
 $("l2").textContent=`Mapas únicos (mapa+mods) já categorizados: ${fmt(saved)} de ${fmt(S.pairs_total)} · visitas nesta execução: ${fmt(c.visits)}`;$("b2").style.width=(saved/Math.max(1,S.pairs_total)*100)+"%";
 $("tNew").textContent=fmt(c.new);$("tCached").textContent=fmt(c.cached);$("tNoFile").textContent=fmt(c.no_file);$("tErr").textContent=fmt(c.error);
 $("tRate").textContent=S.rate_per_s?S.rate_per_s+"/s":"—";$("tTime").textContent=S.elapsed_s?mm(S.elapsed_s):"—";
 $("rule").textContent="Rating por eixo = "+S.evidence_rule+". Letras esbatidas = poucas plays de evidência (<10).";
 $("players").replaceChildren(...S.players.map((p,i)=>{const tr=el("tr",e&&e.user_id===p.user_id&&run?"cur":"");
  [i+1,p.username||("#"+p.user_id),p.pp!=null?fmt(p.pp):"—",p.global_rank!=null?"#"+fmt(p.global_rank):"—",p.maps_done+"/"+p.n_maps,{queued:"em fila",running:"a correr",done:"feito"}[p.status]].forEach((v,j)=>tr.appendChild(el("td",j===2||j===3?"mono":"",v)));
  AX.forEach(([k])=>{const g=p.profile&&p.profile.ratings[k+"_grade"];const td=el("td");const s=el("span","g "+gc(g),g||"–");if(p.profile&&!p.profile.ratings.confident)s.style.opacity=".5";td.appendChild(s);tr.appendChild(td)});return tr}));
 $("feed").replaceChildren(...S.events.slice(0,45).map(v=>{const tr=el("tr",v.cached?"faded":"");
  tr.appendChild(el("td","",(v.username||"#"+v.user_id)+(v.pp!=null?" · "+fmt(v.pp)+" pp":"")));tr.appendChild(el("td","",(v.title||"#"+v.beatmap_id)+" ["+(v.version||"?")+"]"+(v.mods?" +"+v.mods:"")));
  const td=el("td");if(v.status==="ok")AX.forEach(([k])=>td.appendChild(el("span","g "+gc(v.scores[k+"_grade"]),v.scores[k+"_grade"])));else td.appendChild(el("span","chip "+v.status,v.status==="no_file"?"sem .osu":"erro"));
  tr.appendChild(td);tr.appendChild(el("td","dim",v.cached?"cache":"nova"));return tr}));
 const k=S.correlations;$("corr").textContent=k.n<10?`Ainda sem dados suficientes (${k.n} jogadores).`:`n=${k.n} · `+[["aim","Aim"],["speed","Speed"],["stamina","Stamina"],["reading","Reading"],["stars","★"]].map(([a,l])=>`${l}: ${k[a]==null?"—":k[a]}`).join(" · ")+" — perto de +1 = o rating ordena os jogadores como o pp; perto de 0 = não acompanha.";
}
async function refresh(){try{S=await(await fetch("/api/state")).json();render()}catch(e){$("msg").textContent="sem ligação ao servidor"}}
refresh();setInterval(refresh,500);
</script></body></html>"""


def actions(controller: CategorizeController) -> dict[str, Callable[[dict], Any]]:
    def start(body: dict) -> dict:
        err = controller.start(float(body.get("delay_ms") or 0))
        return {"error": err} if err else {"ok": True}

    return {
        "/api/start": start,
        "/api/pause": lambda body: controller.pause(),
        "/api/resume": lambda body: controller.resume(),
        "/api/cancel": lambda body: controller.cancel(),
        "/api/speed": lambda body: controller.set_delay(float(body.get("delay_ms") or 0)),
    }


def make_server(controller: CategorizeController, port: int) -> tuple[ThreadingHTTPServer, str]:
    return make_generic_server(PAGE, controller.state, actions(controller), port)
