"""Página "Evolução" (`/evolution`): pensada primeiro para o telemóvel (uma coluna, alvos de toque grandes) e
alargada para PC (grelha a partir de 900 px). Só lê (0 pedidos à API); o estado vem de `Evolution.state`."""

from __future__ import annotations

from typing import Any, Callable

PAGE = r"""<!doctype html>
<html lang="pt"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Evolução</title>
<style>
:root{--bg:#f5f2ea;--surface:#fff;--surface2:#ece6d6;--border:#ddd4bd;--text:#221f2b;--dim:#67617a;--accent:#a8386c;
--good:#2f8f57;--warn:#b9791f;--bad:#c14545;--aim:#6f5fc9;--speed:#1f8f86;--stamina:#c9622f;--reading:#2f74b8;--stars:#8a7a2c;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#14131a;--surface:#1c1b25;--surface2:#26242f;--border:#383642;--text:#eeecf5;--dim:#a29dbb;
--accent:#e483ac;--good:#4fbf80;--warn:#dba13f;--bad:#e2726f;--aim:#a597ee;--speed:#4fc2b7;--stamina:#f0925e;--reading:#5fa4ea;--stars:#d3c25b;color-scheme:dark}}
*{box-sizing:border-box}html,body{margin:0}
body{background:var(--bg);color:var(--text);font:15px/1.4 system-ui,sans-serif;padding:max(16px,env(safe-area-inset-top)) 16px max(24px,env(safe-area-inset-bottom))}
.wrap{max-width:1100px;margin:0 auto;display:grid;gap:12px;grid-template-columns:1fr}
@media(min-width:900px){.wrap{grid-template-columns:1fr 1fr;gap:14px}.span2{grid-column:1/-1}}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:14px 16px;min-width:0}
h1{font-size:22px;margin:0}h2{font-size:12px;margin:0 0 10px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);font-weight:600}
.dim{color:var(--dim)}.mono{font-family:ui-monospace,monospace}.sub{color:var(--dim);font-size:13px}
.big{font:700 28px ui-monospace,monospace}.row{display:flex;align-items:baseline;justify-content:space-between;gap:10px;flex-wrap:wrap}
.banner{border-radius:12px;padding:10px 14px;background:color-mix(in srgb,var(--warn) 16%,transparent);border:1px solid var(--warn);font-size:14px}
.banner.bad{background:color-mix(in srgb,var(--bad) 14%,transparent);border-color:var(--bad)}
.prog{height:12px;border-radius:99px;background:var(--surface2);overflow:hidden;margin:10px 0 4px}.prog i{display:block;height:100%;background:var(--accent);min-width:3px}
.axes{display:grid;gap:10px;grid-template-columns:1fr}@media(min-width:560px){.axes{grid-template-columns:1fr 1fr}}
.ax{border:1px solid var(--border);border-top:4px solid var(--c);border-radius:12px;padding:10px 12px}
.ax .hd{display:flex;justify-content:space-between;align-items:baseline}.ax .lbl{font-size:12px;text-transform:uppercase;color:var(--dim);letter-spacing:.04em}
.ax b{font:700 26px ui-monospace,monospace}.tr{font:600 13px ui-monospace,monospace}
.tr.sobe{color:var(--good)}.tr.desce{color:var(--bad)}.tr.estavel,.tr.sem_dados{color:var(--dim)}
svg.spark{width:100%;height:56px;display:block;margin-top:6px}
.week{display:flex;gap:6px;margin:8px 0}.d{flex:1;text-align:center;font-size:11px;color:var(--dim)}
.d i{display:block;height:34px;border-radius:8px;background:var(--surface2);margin-bottom:3px}.d.on i{background:var(--good)}.d.today i{outline:2px solid var(--accent)}
.bars{display:flex;gap:3px;align-items:flex-end;height:44px;margin-top:8px}.bars i{flex:1;border-radius:3px;background:var(--surface2);min-height:3px}.bars i.met{background:var(--good)}.bars i.part{background:var(--warn)}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:7px 4px;border-bottom:1px solid var(--border);text-align:right}th:first-child,td:first-child{text-align:left}th{font-size:11px;color:var(--dim);text-transform:uppercase}
.gap.pos{color:var(--bad)}.gap.neg{color:var(--good)}
.note{font-size:12px;color:var(--dim);margin-top:8px}
a{color:var(--accent);text-decoration:none}.map{padding:9px 0;border-bottom:1px solid var(--border)}.map:last-child{border:0}
.map .t{font-weight:600;overflow-wrap:anywhere;display:block}.chips{display:flex;gap:5px;flex-wrap:wrap;margin-top:4px}
.chip{font:600 12px ui-monospace,monospace;padding:2px 8px;border-radius:99px;background:var(--surface2)}.chip.up{color:var(--bad)}.chip.dn{color:var(--good)}
.chip.fail{color:var(--bad)}.chip.pass{color:var(--good)}
.sess{border-left:4px solid var(--accent);padding-left:10px;margin:12px 0}.sess h3{font-size:15px;margin:0 0 2px}
.blk{font-size:13px;margin:6px 0}.blk b{color:var(--text)}ul.rules{margin:4px 0 0;padding-left:18px;font-size:13px;color:var(--dim)}
.tag{font:600 11px ui-monospace,monospace;padding:2px 8px;border-radius:99px;background:var(--surface2);color:var(--dim)}
.tabs{position:sticky;top:0;z-index:5;display:flex;gap:6px;padding:8px 0;background:var(--bg);grid-column:1/-1}
.tabs button{flex:1;font:600 14px system-ui;padding:11px 6px;border-radius:12px;border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer;min-height:44px}
.tabs button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.btn{font:600 14px system-ui;padding:10px 14px;border-radius:12px;border:1px solid var(--border);background:var(--surface2);color:var(--text);cursor:pointer;min-height:44px}
.btn:disabled{opacity:.5}
.fb{display:flex;gap:8px;margin-top:8px}.fb .btn{flex:1;padding:8px 10px;min-height:40px}.fb .btn.on.ok{background:var(--good);border-color:var(--good);color:#fff}.fb .btn.on.no{background:var(--bad);border-color:var(--bad);color:#fff}
details.sess>summary{cursor:pointer;list-style:none;min-height:44px;display:flex;align-items:center;justify-content:space-between;gap:8px}details.sess>summary::-webkit-details-marker{display:none}
details.sess>summary h3{margin:0}.bell{position:relative}.bell b{position:absolute;top:-6px;right:-6px;background:var(--bad);color:#fff;border-radius:99px;font-size:11px;padding:1px 6px}
.nt{padding:8px 0;border-bottom:1px solid var(--border)}.nt.unread b{color:var(--accent)}.nt:last-child{border:0}
.hero{border-left:6px solid var(--accent)}
</style></head><body><div class="wrap" id="app"><div class="card span2 sub">A carregar…</div></div>
<script>
const $=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined&&x!==null)e.textContent=x;return e};
const AX=[["aim","Aim","--aim"],["speed","Speed","--speed"],["stamina","Stamina","--stamina"],["reading","Reading","--reading"]];
const n0=v=>v==null?"—":Math.round(v).toLocaleString("pt-PT"),n1=v=>v==null?"—":Number(v).toFixed(1).replace(".",",");
const ARROW={sobe:"▲",desce:"▼",estavel:"●",sem_dados:"–"};
const TXT={sobe:"a subir",desce:"a descer",estavel:"estável",sem_dados:"poucos dados"};
function spark(points,color){
 const W=300,H=56,P=4,vals=points.map(p=>p.level),ok=vals.filter(v=>v!=null);
 const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.setAttribute("class","spark");
 if(ok.length<2)return svg;
 const lo=Math.min(...ok),hi=Math.max(...ok),span=Math.max(hi-lo,4);
 const x=i=>P+i*(W-2*P)/(points.length-1),y=v=>H-P-(v-lo)/span*(H-2*P);
 let d="",pen=false;vals.forEach((v,i)=>{if(v==null){pen=false;return}d+=(pen?"L":"M")+x(i).toFixed(1)+" "+y(v).toFixed(1)+" ";pen=true});
 const p=document.createElementNS(svg.namespaceURI,"path");p.setAttribute("d",d);p.setAttribute("fill","none");p.setAttribute("stroke",color);p.setAttribute("stroke-width","2.5");p.setAttribute("stroke-linejoin","round");svg.appendChild(p);
 const li=vals.map((v,i)=>v==null?-1:i).filter(i=>i>=0).pop(),c=document.createElementNS(svg.namespaceURI,"circle");
 c.setAttribute("cx",x(li));c.setAttribute("cy",y(vals[li]));c.setAttribute("r","4");c.setAttribute("fill",color);svg.appendChild(c);return svg}
const TOKEN="__TOKEN__";let TAB="hoje",LAST=null;try{TAB=localStorage.getItem("evoTab")||"hoje"}catch(e){}
function saveTab(){try{localStorage.setItem("evoTab",TAB)}catch(e){}}
async function post(path,body){const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Panel-Token":TOKEN},body:JSON.stringify(body||{})});return r.json()}
function headerCard(s){
 const p=s.player,g=s.goal,q=s.data_quality,c=s.consistency,nt=s.notifications||{items:[],unread:0};
 const head=$("div","card span2"),r=$("div","row");r.append($("h1","",p.username),$("span","sub",p.country_rank?`#${n0(p.country_rank)} ${p.country||""} · global #${n0(p.global_rank)}`:`global #${n0(p.global_rank)}`));head.appendChild(r);
 if(g.target_pp){const pct=Math.min(100,(g.progress||0)*100),pr=$("div","prog"),i=$("i");i.style.width=pct+"%";pr.appendChild(i);head.appendChild(pr);
  const rr=$("div","row");rr.append($("span","mono",`${n0(p.pp)} pp`),$("span","sub",`${g.label}: faltam ${n0(g.gap_pp)} pp · ${pct.toFixed(0)} %`));head.appendChild(rr)}
 const bar=$("div","row");bar.style.marginTop="10px";
 const upd=$("button","btn","Atualizar jogadas");upd.title="1 pedido à osu!API (com as proteções do poll)";
 upd.onclick=async()=>{upd.disabled=true;upd.textContent="a verificar…";const res=await post("/api/check");upd.textContent=res.error?res.error:`${res.new_scores||0} jogada(s) nova(s)`;setTimeout(load,800)};
 const bell=$("button","btn bell","Avisos");if(nt.unread)bell.appendChild($("b","",String(nt.unread)));
 bell.onclick=()=>{TAB="avisos";saveTab();render(LAST);post("/api/read")};
 bar.append(upd,bell);head.appendChild(bar);
 if(q.stale)head.appendChild($("div","banner",c.days_since_last==null?"Sem jogadas na base de dados.":`Última jogada há ${c.days_since_last} dias. Depois de jogares, "Atualizar jogadas".`));
 if(s.live&&s.live.at)head.appendChild($("div","note",`Calibração atualizada às ${s.live.at.slice(11,16)}.`));
 return head}
function render(s){
 LAST=s;const app=document.getElementById("app");app.replaceChildren();
 if(s.error){app.appendChild($("div","card span2 banner bad",s.error));return}
 app.appendChild(headerCard(s));
 const tabs=$("div","tabs");[["hoje","Hoje"],["semana","Semana"],["progresso","Progresso"],["rival","Rival"]].forEach(([k,t])=>{const b=$("button",TAB===k?"on":"",t);b.onclick=()=>{TAB=k;saveTab();render(LAST)};tabs.appendChild(b)});app.appendChild(tabs);
 if(TAB==="hoje")tabHoje(app,s);else if(TAB==="semana")tabSemana(app,s);else if(TAB==="progresso")tabProgresso(app,s);else if(TAB==="rival")tabRival(app,s);else tabAvisos(app,s)}
function tabAvisos(app,s){const c=$("div","card span2");c.appendChild($("h2","","Avisos"));const it=(s.notifications||{}).items||[];
 if(!it.length)c.appendChild($("div","sub","Sem avisos."));it.forEach(i=>{const d=$("div","nt"+(i.read?"":" unread"));d.append($("b","",i.title),$("div","sub",i.body),$("div","note",i.at.replace("T"," ")+(i.pushed?" · enviado ao telemóvel":"")));c.appendChild(d)});app.appendChild(c)}
function nextSession(w){return w.sessions.find(x=>x.progress.done<x.progress.total)||null}
function weekDots(c){const wk=$("div","week"),mon=new Date(c.weeks[c.weeks.length-1].week+"T00:00:00");const todayIso=new Date().toISOString().slice(0,10);
 ["S","T","Q","Q","S","S","D"].forEach((l,k)=>{const d=new Date(mon);d.setDate(d.getDate()+k);const iso=d.toISOString().slice(0,10);
  const e=$("div","d"+(c.this_week.includes(iso)?" on":"")+(iso===todayIso?" today":""));e.append($("i"),$("span","",l));wk.appendChild(e)});return wk}
function tabHoje(app,s){
 const w=s.week,c=s.consistency;
 const cc=$("div","card");cc.appendChild($("h2","","Esta semana"));
 const rw=$("div","row");rw.append($("span","big",`${c.this_week_days}/${c.weekly_days_goal}`),$("span","sub",`dias com jogo · sequência ${c.streak_weeks} sem.`));cc.appendChild(rw);cc.appendChild(weekDots(c));app.appendChild(cc);
 if(!w||w.status==="error"||w.status==="unavailable"){app.appendChild($("div","card sub",(w&&w.message)||"Plano da semana indisponível."));return}
 if(w.status==="building"){app.appendChild($("div","card sub","A preparar o plano da semana (alguns segundos)…"));setTimeout(load,6000);return}
 const S=nextSession(w),sc=$("div","card hero");
 if(!S){sc.appendChild($("h2","","Plano da semana"));sc.appendChild($("div","sub","Todas as sessões estão concluídas. Boa semana!"))}
 else{sc.appendChild($("h2","","A seguir"));const h=$("div","row");h.append($("h3","",`Sessão ${S.key} · ${S.title}`),$("span","tag",`${S.progress.done}/${S.progress.total} · ${S.minutes} min`));sc.appendChild(h);
  sc.appendChild($("div","sub",S.goal));const wu=S.blocks.find(b=>b.name==="Aquecimento");if(wu)sc.appendChild($("div","note","Aquecimento: "+wu.what));
  if(!S.maps.length)sc.appendChild($("div","note",S.note||"Sem mapas dentro dos teus limites."));S.maps.forEach(m=>sc.appendChild(planRow(m,false,"sessão "+S.key)));
  const ul=$("ul","rules");S.rules.slice(0,3).forEach(r=>ul.appendChild($("li","",r)));sc.appendChild(ul)}
 app.appendChild(sc);
 const t=$("div","card"),sm=w.test.summary,tr=$("div","row");tr.append($("h2","","Teste semanal"),$("span","tag",`${sm.done}/${sm.total}`));t.appendChild(tr);
 t.appendChild($("div","sub",sm.done?`1.ª jogada: passaste ${sm.first_pass}/${sm.done} · accuracy mediana ${pc(sm.first_acc_median)}`:"Mapas nunca jogados. Só conta a 1.ª jogada de cada um."));
 w.test.maps.forEach(m=>t.appendChild(planRow(m,true,"teste")));app.appendChild(t)}
function tabProgresso(app,s){
 const c=s.consistency,q=s.data_quality,nt0=s.notifications||{};
 const cc=$("div","card span2");cc.appendChild($("h2","","Consistência"));
 const bars=$("div","bars");c.weeks.forEach(w=>{const i=$("i",w.met?"met":(w.days>0?"part":""));i.style.height=Math.max(8,w.days/7*100)+"%";i.title=`${w.week}: ${w.days} dias`;bars.appendChild(i)});
 cc.appendChild(bars);cc.appendChild($("div","note",`Últimas 12 semanas (verde = meta de ${c.weekly_days_goal} dias cumprida).`));app.appendChild(cc);
 const ac=$("div","card span2");ac.appendChild($("h2","",`Nível por eixo (janela de ${q.window_weeks} semanas)`));
 const grid=$("div","axes");
 AX.forEach(([k,label,col])=>{const t=s.trends[k],lv=s.levels[k],a=$("div","ax");a.style.setProperty("--c",`var(${col})`);
  const hd=$("div","hd");hd.append($("span","lbl",label),$("b","",lv==null?"—":n1(lv)));a.appendChild(hd);
  const age=s.levels_age_weeks[k];if(age>0)hd.appendChild($("span","sub",`há ${age} sem.`));
  a.appendChild($("div","tr "+t.direction,`${ARROW[t.direction]} ${TXT[t.direction]}${t.delta!=null?` (${t.delta>0?"+":""}${n1(t.delta)} em 4 sem.)`:""}`));
  a.appendChild(spark(s.series[k],getComputedStyle(document.documentElement).getPropertyValue(col).trim()||"#888"));grid.appendChild(a)});
 ac.appendChild(grid);ac.appendChild($("div","note",(s.goal.measured_at?`Meta medida em ${s.goal.measured_at} (muda com o tempo). `:"")+(nt0.push?"Avisos também no telemóvel (ntfy). ":"Avisos só nesta página (ntfy desligado). ")+`Só conta passes com accuracy ≥ 90 %. Variações abaixo de 1,5 pontos são ruído ("estável"). Jogadas na janela: ${q.plays_in_window}.`));app.appendChild(ac)}
function tabRival(app,s){
 const rv=s.rival,rc=$("div","card span2");rc.appendChild($("h2","","Rival"));
 if(!rv){rc.appendChild($("div","sub","Ainda sem rival: faltam níveis fiáveis nos 4 eixos."));app.appendChild(rc);return}
 const t=$("div","row");t.append($("b","",rv.username),$("span","sub",`${n0(rv.pp)} pp · #${n0(rv.global_rank)}`));rc.appendChild(t);
 const tb=document.createElement("table"),th=document.createElement("tr");["Eixo","Tu","Ele","Diferença"].forEach(h=>th.appendChild($("th","",h)));tb.appendChild(th);
 AX.forEach(([k,label])=>{const x=rv.axes[k],tr2=document.createElement("tr");
  tr2.append($("td","",label),$("td","mono",n1(x.me)),$("td","mono",n1(x.them)),$("td","mono gap "+(x.gap>0?"pos":"neg"),(x.gap>0?"+":"")+n1(x.gap)));tb.appendChild(tr2)});
 rc.appendChild(tb);rc.appendChild($("div","note","Diferença positiva (vermelho) = onde ele está à frente."));app.appendChild(rc);
 if(rv.detail&&rv.detail.maps)rivalDetail(app,rv)}
const ml=id=>"https://osu.ppy.sh/beatmaps/"+id,acc=v=>(v*100).toFixed(1).replace(".",",")+" %";
function mapRow(m,chips){const r=$("div","map"),a=$("a","t",m.map);a.href=ml(m.beatmap_id);a.target="_blank";a.rel="noopener";r.appendChild(a);
 const c=$("div","chips");chips.forEach(x=>c.appendChild($("span","chip"+(x[1]?" "+x[1]:""),x[0])));r.appendChild(c);return r}
function rivalDetail(app,rv){
 const d=rv.detail,mc=$("div","card");mc.appendChild($("h2","",`Mapas dele para ti (${d.maps.length} de ${d.maps_total})`));
 if(!d.maps.length)mc.appendChild($("div","sub","Sem mapas dele ao teu alcance que ainda não tenhas jogado."));
 d.maps.forEach(m=>{const chips=[[`${n0(m.pp)} pp dele`],[acc(m.accuracy)]];if(m.stars)chips.push([`${n1(m.stars)}★`]);
  AX.forEach(([k,l])=>{const g=m.gaps[k];if(g!=null)chips.push([`${l.slice(0,3)} ${g>0?"+":""}${n1(g)}`,g>2?"up":g<-2?"dn":""])});mc.appendChild(mapRow(m,chips))});
 mc.appendChild($("div","note","Sem mods, accuracy dele ≥ 90 %, nunca jogados por ti, até 8 pontos de diferença em qualquer eixo. + = mais difícil que o teu nível, − = mais fácil."));app.appendChild(mc);
 const rc=$("div","card");rc.appendChild($("h2","","Últimas jogadas dele"));
 d.recent.forEach(p=>{const chips=[[p.passed?"PASS":"FAIL",p.passed?"pass":"fail"],[acc(p.accuracy)]];if(p.passed&&p.pp)chips.push([`${n0(p.pp)} pp`]);
  if(p.stars)chips.push([`${n1(p.stars)}★`]);chips.push([String(p.when).slice(0,10)]);rc.appendChild(mapRow(p,chips))});app.appendChild(rc);
 const tc=$("div","card");tc.appendChild($("h2","","Está a melhorar? (nível dele por mês)"));
 const tb=document.createElement("table"),h=document.createElement("tr");["Mês","Aim","Speed","Stam.","Read.","n"].forEach(x=>h.appendChild($("th","",x)));tb.appendChild(h);
 d.monthly_levels.forEach(r=>{const t=document.createElement("tr");t.append($("td","",r.month),...["aim","speed","stamina","reading"].map(k=>$("td","mono",n1(r[k]))),$("td","mono dim",r.n));tb.appendChild(t)});tc.appendChild(tb);
 if(d.rank_90d)tc.appendChild($("div","note",`Rank global em 90 dias: #${n0(d.rank_90d.from)} → #${n0(d.rank_90d.to)}`));
 if(d.plays_per_month.length)tc.appendChild($("div","note","Jogadas/mês: "+d.plays_per_month.map(x=>`${x.month.slice(5)}: ${n0(x.count)}`).join(" · ")));
 if(d.fail_rate!=null)tc.appendChild($("div","note",`Taxa de falhas nas jogadas guardadas: ${Math.round(d.fail_rate*100)} %`));
 tc.appendChild($("div","note","Meses com poucas jogadas (n baixo) são ruído. Só contam passes com accuracy ≥ 90 % em mapas que temos no dump; mapas mais recentes que o dump ficam de fora."));app.appendChild(tc)}
const pc=v=>v==null?"—":(v*100).toFixed(1).replace(".",",")+" %";
const chip=(t,c)=>$("span","chip"+(c?" "+c:""),t);
function planRow(m,test,kind){
 const r=$("div","map"),a=$("a","t",m.label);a.href=m.url;a.target="_blank";a.rel="noopener";r.appendChild(a);
 const c=$("div","chips");c.appendChild(chip(`${n1(m.stars)}★`));if(m.ar)c.appendChild(chip("AR "+n1(m.ar)));if(m.mods)c.appendChild(chip("+"+m.mods));
 if(m.best_acc&&!(m.status&&m.status.played))c.appendChild(chip("tens "+pc(m.best_acc)));if(m.target)c.appendChild(chip("alvo "+pc(m.target)));
 if(!test&&m.p_pass!=null&&!m.best_acc)c.appendChild(chip("P(passar) "+Math.round(m.p_pass*100)+" %"));if(m.dominant)c.appendChild(chip(m.dominant));
 const st=m.status;
 if(st&&st.played){if(test)c.appendChild(chip(`1.ª jogada: ${pc(st.first_acc)} ${st.first_passed?"✓":"✗"}`,st.first_passed?"pass":"fail"));
  else c.appendChild(chip(`${st.attempts} tent. · melhor ${pc(st.best_acc)}${st.passed?" ✓":" ✗"}`,st.passed?"pass":"fail"))}
 else c.appendChild(chip("por jogar"));
 r.appendChild(c);
 if(kind&&m.beatmap_id){const f=$("div","fb"),mk=(v,t,cls)=>{const b=$("button","btn "+cls+(m.verdict===v?" on":""),t);
   b.onclick=async()=>{b.disabled=true;const res=await post("/api/feedback",{beatmap_id:m.beatmap_id,verdict:v,kind:kind});if(!res.error){m.verdict=v;[...f.children].forEach(x=>x.classList.remove("on"));b.classList.add("on")}b.disabled=false};return b};
  f.append(mk("serve","👍 Serve","ok"),mk("nao_serve","👎 Não serve","no"));r.appendChild(f)}
 return r}
function tabSemana(app,s){
 const w=s.week;if(!w){app.appendChild($("div","card span2 sub","Plano da semana indisponível."));return}
 if(w.status==="building"){app.appendChild($("div","card span2 sub","A preparar o plano da semana…"));setTimeout(load,6000)}
 else if(w.status!=="ready"){app.appendChild($("div","card span2 sub",w.message||"Plano da semana indisponível."))}
 const t=$("div","card span2"),tr=$("div","row");tr.append($("h2","","Teste semanal"),$("span","tag",`${w.week} · faixa ${w.band?n1(w.band[0])+"–"+n1(w.band[1])+"★":"—"}`));t.appendChild(tr);
 if(w.status==="ready"){const sm=w.test.summary,row=$("div","row");row.append($("span","big",`${sm.done}/${sm.total}`),$("span","sub",sm.done?`1.ª jogada: passaste ${sm.first_pass}/${sm.done} · accuracy mediana ${pc(sm.first_acc_median)}`:"ainda nenhum jogado"));t.appendChild(row);
  t.appendChild($("div","note",w.test.how));w.test.maps.forEach(m=>t.appendChild(planRow(m,true,"teste")))}
 if(w.series&&w.series.length){
  const pts=w.series.map(x=>({level:x.acc_median==null?null:x.acc_median*100,n:x.n}));
  const box=$("div");box.appendChild($("h2","","Mapas novos na faixa, por semana (accuracy mediana da 1.ª jogada)"));box.style.marginTop="14px";
  if(pts.filter(x=>x.level!=null).length>=2)box.appendChild(spark(pts,"#a8386c"));
  box.appendChild($("div","note",w.series.filter(x=>x.n>0).slice(-4).map(x=>`${x.week.slice(5)}: ${x.n} mapa(s)${x.acc_median!=null?" · "+pc(x.acc_median):""}`).join("  |  ")||"Sem mapas novos nesta faixa nas últimas semanas."));
  if(w.history&&w.history.length){const h=w.history.filter(x=>x.done>0).slice(-4).map(x=>`${x.week}: ${x.done}/${x.total} · ${pc(x.first_acc_median)}`).join("  |  ");if(h)box.appendChild($("div","note","Testes anteriores: "+h))}
  t.appendChild(box)}
 app.appendChild(t);if(w.status!=="ready")return;
 const sc=$("div","card span2");sc.appendChild($("h2","","Sessões de treino · ~20 min cada"));
 sc.appendChild($("div","note",`Limites (medidos nos teus passes): AR efetivo ≤ ${w.limits.ar_cap}${w.limits.stars_ceiling_dt?` · DT até ${n1(w.limits.stars_ceiling_dt)}★`:""}. Sem mods: P90 ${n1(w.stats.s90)}★, máximo passado ${n1(w.stats.s_max)}★.`));
 const cur=nextSession(w);
 w.sessions.forEach(S=>{
  const d=document.createElement("details");d.className="sess";if(cur&&cur.key===S.key)d.open=true;
  const sm=document.createElement("summary"),done=S.progress.done>=S.progress.total&&S.progress.total>0;
  sm.append($("h3","",`${done?"✓ ":""}Sessão ${S.key} · ${S.title}`),$("span","tag",`${S.progress.done}/${S.progress.total} · ${S.minutes} min`));d.appendChild(sm);
  d.appendChild($("div","sub",S.goal));
  S.blocks.forEach(b=>{const e=$("div","blk");e.appendChild($("b","",`${b.name} (${b.minutes} min): `));e.appendChild(document.createTextNode(b.what));d.appendChild(e);
   if(b.maps&&b.maps.length&&b.name==="Aquecimento")b.maps.forEach(m=>d.appendChild(planRow({...m,status:null},false,null)))});
  d.appendChild($("div","blk")).appendChild($("b","","Mapas do bloco principal:"));
  if(!S.maps.length)d.appendChild($("div","note",S.note||"Sem mapas dentro dos teus limites para esta sessão."));
  S.maps.forEach(m=>d.appendChild(planRow(m,false,"sessão "+S.key)));
  if(S.alternative&&S.alternative.length){d.appendChild($("div","note","Alternativa:"));S.alternative.forEach(m=>d.appendChild(planRow({...m,status:null},false,"sessão "+S.key)))}
  const ul=$("ul","rules");S.rules.forEach(r=>ul.appendChild($("li","",r)));d.appendChild(ul);sc.appendChild(d)});
 app.appendChild(sc)}
async function load(){try{render(await(await fetch("/api/state")).json())}catch(e){document.getElementById("app").textContent="sem ligação ao servidor"}}
load();setInterval(load,60000);
</script></body></html>"""


def actions(evolution: Any) -> dict[str, Callable[[dict], Any]]:
    return {"/api/feedback": evolution.give_feedback, "/api/check": evolution.check_now, "/api/read": evolution.read_notifications}
