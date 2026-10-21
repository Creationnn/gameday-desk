"use strict";
/* Gameday Desk features layered on the core app (index.html):
   usage trends, playoff schedule strength, waiver plans, trade finder, live game day,
   source accuracy, and the draft board. Uses the core's globals (S, api, bestLineup, rate, ...). */

/* ---------- speed: rest-of-season values are reused thousands of times by the waiver and trade scans ---------- */
const _rateRaw = rate;
let rateCache = new Map();
rate = function(id){ let v = rateCache.get(id); if(v===undefined){ v = _rateRaw(id); rateCache.set(id, v); } return v; };

/* ---------- usage trends ---------- */
const pct = x => Math.round(x*100) + "%";
/* Target share bands from 2026 data (10-team PPR): green = starter level, yellow = borderline, gray = bench. */
const TS_BANDS = {WR:[.20,.15], TE:[.15,.10], RB:[.10,.05], QB:[1,1]};
function tsClass(pos, v){ const b = TS_BANDS[pos] || TS_BANDS.WR; return v>=b[0] ? "good" : v>=b[1] ? "warn" : "mut"; }
function usageInfo(id){
  const u = S.usage?.[id]; if(!u) return null;
  const idp = !u.tg;
  const p = S.players[id], pos = p?.pos;
  const avg = a => { const v = a.filter(x=>x!=null); return v.length ? v.reduce((x,y)=>x+y,0)/v.length : null; };
  const lastOf = a => { for(let i=a.length-1;i>=0;i--) if(a[i]!=null) return a[i]; return null; };
  const sn = u.sn.filter(x=>x!=null);
  const last = lastOf(u.sn);
  const earlier = sn.length > 1 ? avg(sn.slice(0,-1)) : null;
  const arrow = earlier==null || last==null ? "" : last-earlier >= .10 ? ` <span class="u">↑</span>` : last-earlier <= -.10 ? ` <span class="d">↓</span>` : "";
  const recent2 = sn.slice(-2), prior2 = sn.slice(0,-2);
  const rising = recent2.length===2 && prior2.length>=1 && last>=.5 && avg(recent2) >= avg(prior2) + .15;
  const parts = [];
  if(last!=null) parts.push(`Snaps ${pct(last)}${arrow}`);
  if(!idp){
    const ts = lastOf(u.ts), tsAvg = avg(u.ts), tg = lastOf(u.tg), ca = lastOf(u.ca);
    if(pos!=="QB" && ts!=null) parts.push(`<span class="ts ${tsClass(pos, tsAvg ?? ts)}">Tgt share ${pct(ts)}${tsAvg!=null && u.ts.filter(x=>x!=null).length>1 ? ` (avg ${pct(tsAvg)})` : ""}</span>`);
    if(pos!=="QB" && tg!=null) parts.push(`${tg} tgt`);
    if(ca!=null && (pos==="RB" || pos==="QB" || ca>0)) parts.push(`${ca} car`);
  }
  if(!parts.length) return null;
  const title = u.wk.map((w,i)=>`Wk ${w}: ${u.sn[i]!=null ? pct(u.sn[i])+" snaps" : "did not play"}${!idp && u.tg[i]!=null ? `, ${u.ts[i]!=null ? pct(u.ts[i])+" tgt share, " : ""}${u.tg[i]} tgt, ${u.ca[i]} car` : ""}`).join("\n");
  return {html: parts.join(" · "), title, rising, last};
}

/* ---------- schedule: byes and playoff strength ---------- */
const playoffStart = () => S.league?.settings?.playoff_week_start || 15;
function plays(team, w){
  if(w===S.week) return !S.playing || !S.playing.size || S.playing.has(team);
  const sc = S.cons?.sched; if(!sc) return true;
  return !!sc[team]?.[w];
}
function playoffSOS(id){
  const p = S.players[id], sc = S.cons?.sched, of = S.cons?.oppf;
  if(!p || !sc || !of || !p.team) return null;
  const P = playoffStart(), out = [];
  for(const w of [P, P+1, P+2]){
    const o = sc[p.team]?.[w]; const f = o && of[p.pos]?.[o];
    if(o && f) out.push({w, o, f});
  }
  if(!out.length) return null;
  const avg = out.reduce((a,x)=>a+x.f,0)/out.length;
  const label = avg>=1.07 ? "EASY" : avg<=0.93 ? "HARD" : "";
  return {avg, label, cls: label==="EASY" ? "good" : "bad",
          title: "Fantasy playoffs: " + out.map(x=>`Wk ${x.w} vs ${x.o} (${x.f>1?"+":""}${Math.round((x.f-1)*100)}%)`).join(", ")};
}
/* Average starting-lineup points per week for the rest of the regular season, with byes zeroed. */
function rosWeeks(){
  const P = playoffStart(), out = [];
  for(let w=S.week+1; w<P; w++) out.push(w);
  return out.length ? out : [S.week+1, S.week+2, S.week+3].filter(w=>w<=17);
}
function rosLineup(ids){
  const weeks = rosWeeks(); if(!weeks.length) return lineupTotal(bestLineup(ids, S.slots, S.players, rate), rate);
  const cache = new Map(); let tot = 0;
  for(const w of weeks){
    const off = ids.filter(id=>!plays(S.players[id]?.team, w));
    const key = off.sort().join(",");
    if(!cache.has(key)){
      const offSet = new Set(off), fn = id => offSet.has(id) ? 0 : rate(id);
      cache.set(key, lineupTotal(bestLineup(ids, S.slots, S.players, fn), fn));
    }
    tot += cache.get(key);
  }
  return tot/weeks.length;
}
const weekLineup = ids => lineupTotal(bestLineup(ids, S.slots, S.players, weekVal), weekVal);

/* ---------- waivers: best add/drop moves ---------- */
const _renderWaivers = renderWaivers;
renderWaivers = function(){
  _renderWaivers();
  const el = document.createElement("div"); el.className = "card"; el.id = "bestmoves";
  el.innerHTML = `<h2>Best moves</h2><p class="note">Working out the best pickups for your roster…</p>`;
  $("#p-waivers").prepend(el);
  setTimeout(()=>{ try{ el.innerHTML = bestMovesHtml(); }catch(e){ console.error(e); el.innerHTML = `<h2>Best moves</h2><p class="note">Couldn't work out moves: ${esc(e.message)}</p>`; } }, 0);
};
function bestMovesHtml(){
  const positions = [...new Set(S.slots.flatMap(s=>SLOT_ELIG[s]||[]))];
  const pool = Object.keys(S.players).filter(id=>{
    const p = S.players[id];
    return !S.rostered.has(id) && p.team && p.team!=="FA" && p.fpos.some(x=>positions.includes(x)) && !OUT_STATUSES.has(p.inj);
  });
  const byRate = pool.slice().sort((a,b)=>rate(b)-rate(a)).slice(0,30);
  const byWeek = pool.slice().sort((a,b)=>weekVal(b)-weekVal(a)).slice(0,12);
  const adds = [...new Set([...byRate, ...byWeek])];
  const mine = allMine();
  const baseW = weekLineup(mine), baseR = rosLineup(mine);
  const weeksLeft = rosWeeks().length;
  const best = [];
  for(const a of adds){
    let top = null;
    for(const d of mine){
      const ids = mine.filter(x=>x!==d).concat(a);
      const dW = weekLineup(ids) - baseW, dR = rosLineup(ids) - baseR;
      const total = dW + dR*weeksLeft;
      if(!top || total > top.total) top = {a, d, dW, dR, total};
    }
    if(top && top.total > 1.5 && (top.dR > 0.3 || top.dW > 1)) best.push(top);
  }
  best.sort((x,y)=>y.total-x.total);
  const seenDrop = new Map(), picks = [];
  for(const m of best){ const n = seenDrop.get(m.d)||0; if(n<2){ picks.push(m); seenDrop.set(m.d, n+1); } if(picks.length>=8) break; }
  if(!picks.length) return `<h2>Best moves</h2><p class="note">No free agent improves your lineup right now. Your roster is in good shape.</p>`;
  const sign = v => (v>=0?"+":"") + f1(v);
  return `<h2>Best moves</h2>
    <div class="moves" style="display:flex;flex-direction:column;gap:8px">${picks.map(m=>`
      <div class="idea"><div class="top"><span class="up">Add ${esc(pName(m.a))}</span><span class="dn">drop ${esc(pName(m.d))}</span>
        <span class="gain">${sign(m.dW)} this week · ${sign(m.dR)}/wk after</span></div>
        <div class="note">${playerCell(m.a,{trend:true,po:true})}</div></div>`).join("")}</div>
    <p class="note">Each move is scored on your best possible lineup: this week with the consensus projections, then each remaining regular-season week with bye weeks counted as zero. The list is sorted by total points gained through week ${playoffStart()-1}.</p>`;
}

/* ---------- trade finder ---------- */
const _renderTrade = renderTrade;
renderTrade = function(){
  _renderTrade();
  const el = document.createElement("div"); el.className = "card"; el.id = "tradeideas";
  el.innerHTML = `<h2>Trade ideas</h2><p class="note">Scanning every roster in the league for trades that help both teams…</p>`;
  $("#p-trade").prepend(el);
  setTimeout(()=>{ try{ el.innerHTML = tradeIdeasHtml(); }catch(e){ console.error(e); el.innerHTML = `<h2>Trade ideas</h2><p class="note">Couldn't scan trades: ${esc(e.message)}</p>`; } }, 0);
};
function tradeIdeasHtml(){
  if(S._ideas && S._ideas.key === S.week+"|"+S.league.league_id) return S._ideas.html;
  const val = ids => lineupTotal(bestLineup(ids, S.slots, S.players, rate), rate);
  const active = t => t.players.filter(id=>!t.reserve.includes(id));
  const mine = active(S.me), base = val(mine);
  const useful = ids => ids.filter(id=>rate(id) >= 4).sort((a,b)=>rate(b)-rate(a)).slice(0,14);
  const ideas = [];
  for(const t of S.teams){
    if(t===S.me) continue;
    const theirs = active(t), tBase = val(theirs);
    const A = useful(mine), B = useful(theirs);
    const tryDeal = (give, get) => {
      const g = new Set(give), r = new Set(get);
      const me2 = mine.filter(x=>!g.has(x)).concat(get), th2 = theirs.filter(x=>!r.has(x)).concat(give);
      const dMe = val(me2) - base, dTh = val(th2) - tBase;
      if(dMe > 0.8 && dTh > 0.3) ideas.push({t, give, get, dMe, dTh, score: Math.min(dMe, dTh*1.5) + 0.25*(dMe+dTh)});
    };
    for(const a of A) for(const b of B) tryDeal([a],[b]);
    for(let i=0;i<A.length;i++) for(let j=i+1;j<A.length;j++) for(const b of B) tryDeal([A[i],A[j]],[b]);
    for(const a of A) for(let i=0;i<B.length;i++) for(let j=i+1;j<B.length;j++) tryDeal([a],[B[i],B[j]]);
  }
  // Drop two-for-ones where the extra player adds nothing over the matching one-for-one.
  const key = d => d.t.rid + "|" + [...d.give].sort() + "|" + [...d.get].sort();
  const byKey = new Map(ideas.map(d=>[key(d), d]));
  const redundant = d => {
    const subs = [];
    if(d.give.length===2) d.give.forEach(x=>subs.push({t:d.t, give:d.give.filter(y=>y!==x), get:d.get}));
    if(d.get.length===2) d.get.forEach(x=>subs.push({t:d.t, give:d.give, get:d.get.filter(y=>y!==x)}));
    return subs.some(sd=>{ const o = byKey.get(key(sd)); return o && o.dMe >= d.dMe-0.3 && o.dTh >= d.dTh-0.3; });
  };
  for(let i=ideas.length-1;i>=0;i--) if(redundant(ideas[i])) ideas.splice(i,1);
  ideas.sort((x,y)=>y.score-x.score);
  const perTeam = new Map(), top = [];
  for(const d of ideas){ const n = perTeam.get(d.t)||0; if(n<2){ top.push(d); perTeam.set(d.t,n+1); } if(top.length>=8) break; }
  const names = ids => ids.map(id=>esc(pName(id))).join(" + ");
  const html = top.length ? `<h2>Trade ideas</h2>
    <div style="display:flex;flex-direction:column;gap:8px">${top.map(d=>`
      <div class="idea"><div class="top"><span><b>${esc(d.t.name)}</b></span>
        <span class="gain">you +${f1(d.dMe)}/wk · them +${f1(d.dTh)}/wk</span></div>
        <div>You send <b>${names(d.give)}</b> · you get <b>${names(d.get)}</b></div>
        <div><button type="button" data-load-trade="${d.t.rid}" data-give="${esc(d.give.join(","))}" data-get="${esc(d.get.join(","))}">Check this trade</button></div></div>`).join("")}</div>
    <p class="note">Every one-for-one and two-for-one swap with every team, kept only when both starting lineups get better per week. Trades the other manager gains from are much easier to get accepted. In a two-for-one, the side receiving two players has to drop someone.</p>`
    : `<h2>Trade ideas</h2><p class="note">No trade in the league clearly helps both teams right now.</p>`;
  S._ideas = {key: S.week+"|"+S.league.league_id, html};
  return html;
}
document.addEventListener("click", e=>{
  const b = e.target.closest("button[data-load-trade]"); if(!b) return;
  S.tradeTeam = +b.dataset.loadTrade;
  S.give = new Set(b.dataset.give.split(",").filter(Boolean));
  S.get = new Set(b.dataset.get.split(",").filter(Boolean));
  renderTrade();
  document.querySelector("#p-trade .verdict")?.scrollIntoView({behavior:"smooth", block:"center"});
});
/* Clear cached ideas whenever the league reloads. */
const _loadLeague = loadLeague;
loadLeague = async function(){ S._ideas = null; rateCache = new Map(); await _loadLeague(); };

/* ---------- live game day ---------- */
const ESPN_ABBR = {WSH:"WAS", JAC:"JAX", LA:"LAR"};
async function fetchGames(){
  try{
    const r = await fetch(`https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?seasontype=2&week=${S.week}&dates=${S.season}`, {cache:"no-store"});
    const j = await r.json(), g = {};
    for(const ev of j.events||[]){
      const st = ev.status||{}, type = st.type||{};
      const comp = ev.competitions?.[0]; if(!comp) continue;
      let frac = 0;
      if(type.state==="post") frac = 1;
      else if(type.state==="in"){
        const per = st.period||1, clock = st.clock||0;
        frac = per>4 ? 0.97 : Math.min(0.99, ((per-1)*900 + (900-clock))/3600);
        if(type.name==="STATUS_HALFTIME") frac = 0.5;
      }
      const when = new Date(ev.date);
      const label = type.state==="pre" ? when.toLocaleString([], {weekday:"short", hour:"numeric", minute:"2-digit"})
                  : type.state==="in" ? (type.shortDetail || `Q${st.period} ${st.displayClock}`) : (type.shortDetail || "Final");
      const teams = (comp.competitors||[]).map(c=>({abbr: ESPN_ABBR[c.team?.abbreviation]||c.team?.abbreviation, score: c.score}));
      for(const t of teams) g[t.abbr] = {state: type.state, frac, label, score: teams.map(x=>`${x.abbr} ${x.score}`).join(" – ")};
    }
    S.games = g; S.gamesAt = Date.now();
  }catch(e){ console.warn("scoreboard", e); }
}
const anyLive = () => S.games && Object.values(S.games).some(g=>g.state==="in");
function liveTeam(m){
  let pts = 0, rem = 0, varr = 0;
  for(const id of m.starters||[]){
    if(!id || id==="0") continue;
    const g = S.games?.[S.players[id]?.team];
    const frac = g ? g.frac : 0;
    const p = m.players_points?.[id] || 0, pr = weekVal(id);
    pts += p; rem += pr*(1-frac); varr += Math.pow(0.45*Math.max(pr,2),2)*(1-frac);
  }
  return {pts, rem, total: pts+rem, varr};
}
renderMatchup = function(){
  const mu = myMatchup();
  if(!mu){ $("#p-matchup").innerHTML = `<div class="card empty">No matchup for week ${S.week}. It may be a bye week or the playoffs haven't been set yet.</div>`; return; }
  const {mine, opp, oppTeam} = mu;
  const started = S.games && Object.values(S.games).some(g=>g.state!=="pre");
  const a = liveTeam(mine), b = liveTeam(opp);
  const pre = winProb(mine, opp);
  const sd = Math.sqrt(a.varr + b.varr);
  let win = !started ? pre : sd < 0.01 ? (a.pts>b.pts?1:a.pts<b.pts?0:0.5) : normCdf((a.total-b.total)/sd);
  if(sd >= 0.01) win = Math.min(0.99, Math.max(0.01, win));  // never 0% or 100% while games are left
  const live = anyLive();
  const side = m => S.slots.map((slot,i)=>{
    const id = (m.starters||[])[i] || "0";
    const g = id!=="0" ? S.games?.[S.players[id]?.team] : null;
    const actual = m.players_points?.[id];
    return `<tr><td class="slot">${esc(slot)}</td><td>${playerCell(id)}${g?`<span class="gs">${esc(g.label)}</span>`:""}</td><td class="n">${id!=="0"?f1(weekVal(id)):"–"}</td>${started?`<td class="n">${actual!=null?f1(actual):"–"}</td>`:""}</tr>`;
  }).join("");
  const head = `<thead><tr><th>Slot</th><th>Player</th><th class="n">Proj</th>${started?`<th class="n">Pts</th>`:""}</tr></thead>`;
  const upd = S.gamesAt ? new Date(S.gamesAt).toLocaleTimeString([], {hour:"numeric", minute:"2-digit"}) : "";
  $("#p-matchup").innerHTML = `
    <div class="card">
      ${live ? `<div class="livebadge">● LIVE · updates every minute · last ${esc(upd)}</div>` : ""}
      <div class="vs">
        <div class="side"><div class="t">${esc(S.me.name)}</div><div class="v">${f1(started?a.pts:teamProj(mine))}</div><div class="note">${started?`on pace for ${f1(a.total)}`:"projected"}</div></div>
        <div class="mid">VS</div>
        <div class="side"><div class="t">${esc(oppTeam.name)}</div><div class="v">${f1(started?b.pts:teamProj(opp))}</div><div class="note">${started?`on pace for ${f1(b.total)}`:"projected"}</div></div>
      </div>
      <div class="meter" role="img" aria-label="Win chance ${Math.round(win*100)} percent"><i style="width:${(win*100).toFixed(1)}%"></i></div>
      <p class="note">${started
        ? `Live win chance: <b>${Math.round(win*100)}%</b> (pregame it was ${Math.round(pre*100)}%). "On pace" adds each player's points so far to what's left of his projection, based on the game clock.`
        : `Pregame win chance from projections: <b>${Math.round(win*100)}%</b>. It assumes each player's score swings about 45% around his projection. During games this page updates live.`} Your side uses your currently set lineup.</p>
    </div>
    <div class="grid2">
      <div class="card"><h3>${esc(S.me.name)}</h3><div class="tbl"><table>${head}<tbody>${side(mine)}</tbody></table></div></div>
      <div class="card"><h3>${esc(oppTeam.name)}</h3><div class="tbl"><table>${head}<tbody>${side(opp)}</tbody></table></div></div>
    </div>`;
};
let liveTimer = null;
async function refreshLive(){
  try{
    const [m] = await Promise.all([api(`/v1/league/${S.league.league_id}/matchups/${S.week}`), fetchGames()]);
    if(Array.isArray(m)) S.matchups = m;
    renderMatchup(); renderStrip();
  }catch(e){ console.warn(e); }
  if(!anyLive()){ clearInterval(liveTimer); liveTimer = null; }
}

/* ---------- accuracy ---------- */
const GROUP_ORDER = ["QB","RB","WR","TE","DL","LB","DB"];
function renderAccuracy(){
  const A = S.accuracy?.groups;
  if(!A || !Object.keys(A).length){ $("#p-accuracy").innerHTML = `<div class="card empty">Accuracy grades appear after the first graded week.</div>`; return; }
  const groups = GROUP_ORDER.filter(g=>A[g] && S.slots.some(s=>(SLOT_ELIG[s]||[]).includes(g)));
  const weeks = [...new Set(groups.flatMap(g=>A[g].weeks))].sort((a,b)=>a-b);
  const cards = groups.map(g=>{
    const st = A[g].stats, srcs = Object.keys(st).filter(s=>s!=="consensus");
    const best = Math.min(...Object.values(st).map(x=>x.mae));
    const row = (s, label) => {
      const x = st[s]; const b = x.bias; const off = Math.round((b-1)*100);
      const bias = Math.abs(off) < 3 ? "on target" : off > 0 ? `${off}% too low` : `${-off}% too high`;
      return `<tr class="${x.mae===best?"hi":""}"><td>${label}</td><td class="n">${x.mae.toFixed(2)}</td><td class="n">${bias}</td><td class="n">${s==="consensus"?"–":Math.round((A[g].weights[s]||0)*100)+"%"}</td><td class="n">${x.n}</td></tr>`;
    };
    return `<div class="card"><h3>${g}</h3><div class="tbl"><table>
      <thead><tr><th>Source</th><th class="n">Avg miss</th><th class="n">Bias</th><th class="n">Weight</th><th class="n">Players</th></tr></thead>
      <tbody>${srcs.sort((a,b)=>st[a].mae-st[b].mae).map(s=>row(s, SOURCE_NAMES[s]||s)).join("")}${row("consensus","<b>Consensus</b>")}</tbody></table></div></div>`;
  }).join("");
  $("#p-accuracy").innerHTML = `
    <div class="card"><h2>Source accuracy</h2>
      <p class="note">Every week's projections are saved and graded against what actually happened, scored with ${esc(S.league.name)}'s rules. Graded weeks so far: ${weeks.length ? weeks[0]+"–"+weeks[weeks.length-1] : "none"}. <b>Avg miss</b> is how many fantasy points a projection was off by, on average. <b>Bias</b> shows whether a source runs high or low; the app corrects for it. <b>Weight</b> is how much each source counts in the consensus, and it shifts toward the more accurate sources as more weeks are graded. The best row in each position is highlighted.</p></div>
    <div class="grid2">${cards}</div>`;
}

/* ---------- draft board ---------- */
function renderDraftShell(){
  if(S.draft){ renderDraft(); return; }
  const id = store.get("gd.draft", "") || S.league?.draft_id || "";
  $("#p-draft").innerHTML = `
    <div class="card"><h2>Draft board</h2>
      <form class="row" id="draftform">
        <label class="f" for="draftid">Sleeper draft ID<input id="draftid" value="${esc(id)}" inputmode="numeric" style="width:200px"></label>
        <button class="primary" type="submit">Load draft</button>
      </form>
      <p class="note">Opens on your league's draft. To use it in a mock draft or next season's draft, paste the draft ID from the Sleeper draft page link (the long number after /draft/nfl/). During a live draft the board updates every few seconds.</p>
    </div>`;
}
document.addEventListener("submit", e=>{
  if(e.target.id!=="draftform") return;
  e.preventDefault();
  const id = $("#draftid").value.trim();
  if(id){ store.set("gd.draft", id); loadDraft(id); }
});
let draftTimer = null;
async function loadDraft(id, quiet){
  if(!quiet) $("#p-draft").innerHTML = `<div class="card"><p class="note">Loading draft…</p></div>`;
  try{
    const draft = await api(`/v1/draft/${id}`);
    const picks = await api(`/v1/draft/${id}/picks`);
    if(!S.draft || S.draft.id !== id){
      const league = draft.league_id ? await api(`/v1/league/${draft.league_id}`).catch(()=>S.league) : S.league;
      const scoring = league?.scoring_settings || S.scoring;
      const rp = (league?.roster_positions || S.league.roster_positions);
      const slots = rp.filter(s=>!NON_STARTING.has(s));
      const pos = [...new Set(slots.flatMap(s=>SLOT_ELIG[s]||[]))];
      const q = pos.map(p=>"position[]="+p).join("&");
      const proj = await api(`/projections/nfl/${draft.season}?season_type=regular&${q}`).catch(()=>[]);
      const hasIdp = pos.some(p=>["DL","LB","DB"].includes(p));
      const sf = slots.includes("SUPER_FLEX");
      const rec = scoring.rec || 0;
      const adpKey = sf ? "adp_2qb" : hasIdp ? "adp_idp" : rec>=1 ? "adp_ppr" : rec>=0.5 ? "adp_half_ppr" : "adp_std";
      const players = {};
      for(const e of proj||[]){
        if(!e?.player_id || !e.player) continue;
        const pp = e.player, ppos = POS_NORM[pp.position] || pp.position;
        if(!pos.includes(ppos)) continue;
        const st = e.stats||{};
        const adp = st[adpKey] ?? st.adp_ppr ?? st.adp_std;
        players[e.player_id] = {id:e.player_id, name:`${pp.first_name||""} ${pp.last_name||""}`.trim(), pos:ppos, team:e.team||pp.team||"FA",
          pts: scorePts(st, scoring), adp: adp && adp<900 ? adp : null};
      }
      S.draft = {id, season: draft.season, slots, players, leagueName: league?.name || "", teamsN: draft.settings?.teams || S.teams.length};
      computeDraftValues();
    }
    Object.assign(S.draft, {meta: draft, picks});
    renderDraft();
    clearInterval(draftTimer); draftTimer = null;
    if(draft.status==="drafting" && S.tab==="draft") draftTimer = setInterval(()=>loadDraft(id, true), 5000);
  }catch(e){
    $("#p-draft").innerHTML = `<div class="card"><h2>Draft board</h2><p class="status err">Couldn't load draft ${esc(id)}: ${esc(e.message)}. Check the ID and try again.</p></div>`;
    S.draft = null; setTimeout(renderDraftShell, 4000);
  }
}
function computeDraftValues(){
  const D = S.draft, T = D.teamsN;
  const need = {};
  const share = {FLEX:{RB:.45,WR:.45,TE:.10}, WRRB_FLEX:{WR:.5,RB:.5}, REC_FLEX:{WR:.8,TE:.2}, SUPER_FLEX:{QB:.8,RB:.08,WR:.08,TE:.04}, IDP_FLEX:{LB:.5,DB:.3,DL:.2}};
  for(const s of D.slots){
    if(share[s]) for(const [p,v] of Object.entries(share[s])) need[p] = (need[p]||0) + v;
    else need[s] = (need[s]||0) + 1;
  }
  const byPos = {};
  for(const p of Object.values(D.players)) (byPos[p.pos] ||= []).push(p);
  for(const [pos, list] of Object.entries(byPos)){
    list.sort((a,b)=>b.pts-a.pts);
    const n = Math.round((need[pos]||0)*T);
    const repl = list[n] ? list[n].pts : (list[list.length-1]?.pts || 0);
    let tier = 1;
    list.forEach((p,i)=>{
      if(i>0 && list[i-1].pts - p.pts > Math.max(10, 0.06*list[i-1].pts)) tier++;
      p.vor = p.pts - repl; p.posRank = i+1; p.tier = tier;
    });
  }
  const all = Object.values(D.players).sort((a,b)=>b.vor-a.vor);
  all.forEach((p,i)=>p.rank = i+1);
  D.order = all;
}
function renderDraft(){
  const D = S.draft, meta = D.meta, picks = D.picks||[];
  const T = D.teamsN, taken = new Map(picks.map(p=>[p.player_id, p]));
  const rosterName = rid => S.teams.find(t=>t.rid===rid)?.name || `Team ${rid}`;
  const mySlot = meta.draft_order?.[S.user.user_id];
  const myPicks = picks.filter(p=>p.picked_by===S.user.user_id || (mySlot && p.draft_slot===mySlot));
  const rounds = meta.settings?.rounds || 15;
  const slotPick = (r, s) => meta.type==="snake" && r%2===0 ? (r-1)*T + (T-s+1) : (r-1)*T + s;
  const myNext = mySlot ? Array.from({length:rounds},(_,i)=>slotPick(i+1, mySlot)).find(n=>n>picks.length) : null;
  const pos = [...new Set(D.slots.flatMap(s=>SLOT_ELIG[s]||[]))];
  const filt = S.dpos || "ALL", hide = S.dhide ?? (meta.status!=="complete");
  const list = D.order.filter(p=>(filt==="ALL"||p.pos===filt) && (!hide || !taken.has(p.id))).slice(0, 150);
  const pickLabel = p => `${p.round}.${String(((p.pick_no-1)%T)+1).padStart(2,"0")}`;
  const rows = list.map((p,i)=>{
    const t = taken.get(p.id);
    const prev = list[i-1];
    const brk = filt!=="ALL" && prev && prev.tier!==p.tier;
    const val = p.adp ? Math.round(p.adp - p.rank) : null;
    const isMine = t && myPicks.includes(t);
    return `<tr class="${t?(isMine?"mine":"taken"):""} ${brk?"tierbreak":""}"><td class="n">${p.rank}</td>
      <td><div class="pl"><b>${esc(p.name)}</b><span>${esc(p.pos)}${p.posRank} · ${esc(p.team)} · tier ${p.tier}${t?` · picked ${pickLabel(t)} by ${esc(isMine?"you":rosterName(t.roster_id))}`:""}</span></div></td>
      <td class="n">${f1(p.pts)}</td><td class="n">${f1(p.vor)}</td><td class="n">${p.adp?f1(p.adp):"–"}</td>
      <td class="n">${val==null?"–":val>=10?`<span class="chip good">+${val}</span>`:val<=-10?`<span class="chip bad">${val}</span>`:val}</td></tr>`;
  }).join("");
  // Best available at each position + my roster by position.
  const avail = p => !taken.has(p.id);
  const bestAvail = pos.map(ps=>{
    const top = D.order.filter(p=>p.pos===ps && avail(p)).slice(0,3);
    return `<li><span class="slot">${ps}</span>${top.map(p=>`${esc(p.name)} <span class="note">${f1(p.vor)}</span>`).join(" · ") || "—"}</li>`;
  }).join("");
  const mineByPos = pos.map(ps=>{
    const names = myPicks.map(pk=>D.players[pk.player_id] || {name:`${pk.metadata?.first_name||""} ${pk.metadata?.last_name||""}`, pos: POS_NORM[pk.metadata?.position]||pk.metadata?.position}).filter(p=>p.pos===ps);
    return `<li><span class="slot">${ps}</span>${names.map(p=>esc(p.name)).join(", ") || "<span class='note'>none yet</span>"}</li>`;
  }).join("");
  // Completed draft: projected value drafted by each team.
  let grades = "";
  if(meta.status==="complete"){
    const byRoster = {};
    for(const pk of picks){ const p = D.players[pk.player_id]; byRoster[pk.roster_id] = (byRoster[pk.roster_id]||0) + Math.max(0, p?.vor||0); }
    const ranked = Object.entries(byRoster).sort((a,b)=>b[1]-a[1]);
    grades = `<div class="card"><h3>Projected value drafted, by team</h3><div class="tbl"><table><thead><tr><th class="n">#</th><th>Team</th><th class="n">Value over replacement</th></tr></thead><tbody>
      ${ranked.map(([rid,v],i)=>`<tr class="${+rid===S.me.rid?"hi":""}"><td class="n">${i+1}</td><td>${esc(rosterName(+rid))}</td><td class="n">${f1(v)}</td></tr>`).join("")}</tbody></table></div>
      <p class="note">Uses today's ${esc(D.season)} season projections, so it shows how each team's picks look now, not how they looked on draft day.</p></div>`;
  }
  const status = meta.status==="drafting" ? `<span class="livebadge">● LIVE</span> Pick ${picks.length+1} of ${rounds*T}${myNext?` · your next pick is #${myNext} (${myNext-picks.length-1} picks away)`:""}`
    : meta.status==="complete" ? `This draft is complete (${picks.length} picks).` : `Draft hasn't started yet${myNext?` · your first pick is #${myNext}`:""}.`;
  const chip = (v,l)=>`<button type="button" data-dpos="${v}" aria-pressed="${filt===v}">${l}</button>`;
  $("#p-draft").innerHTML = `
    <div class="card"><div class="row" style="justify-content:space-between"><h2>Draft board · ${esc(D.season)}</h2><button type="button" id="draftchange">Change draft</button></div>
      <div class="note">${status}</div>
      <div class="chips">${chip("ALL","ALL")}${pos.map(p=>chip(p,p)).join("")}
        <label style="display:flex;gap:6px;align-items:center;font-size:13px"><input type="checkbox" id="dhide" ${hide?"checked":""}>Hide drafted</label></div>
      <div class="tbl"><table><thead><tr><th class="n">#</th><th>Player</th><th class="n">Proj pts</th><th class="n">Value</th><th class="n">ADP</th><th class="n">vs ADP</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="6" class="empty">No players to show.</td></tr>`}</tbody></table></div>
      <p class="note"><b>Value</b> is projected season points above a replacement-level starter at the same position (what's left after every team fills its starting spots), so it compares positions fairly. <b>vs ADP</b> shows how much later than his rank a player usually goes; a big plus is a bargain. Tier lines appear when you filter to one position.</p></div>
    <div class="grid2">
      <div class="card"><h3>Best available</h3><ul class="moves">${bestAvail}</ul></div>
      <div class="card"><h3>Your picks</h3><ul class="moves">${mineByPos}</ul></div>
    </div>${grades}`;
}
document.addEventListener("click", e=>{
  const b = e.target.closest("button[data-dpos]"); if(b){ S.dpos = b.dataset.dpos; renderDraft(); return; }
  if(e.target.id==="draftchange"){ clearInterval(draftTimer); S.draft = null; renderDraftShell(); }
});
document.addEventListener("change", e=>{ if(e.target.id==="dhide"){ S.dhide = e.target.checked; renderDraft(); } });

/* ---------- tab lifecycle ---------- */
function onTabShown(t){
  if(t==="matchup"){
    if(!S.gamesAt || Date.now()-S.gamesAt > 60000) fetchGames().then(()=>{ renderMatchup(); if(anyLive() && !liveTimer) liveTimer = setInterval(refreshLive, 60000); });
    else if(anyLive() && !liveTimer) liveTimer = setInterval(refreshLive, 60000);
  } else if(liveTimer){ clearInterval(liveTimer); liveTimer = null; }
  if(t==="draft"){
    if(!S.draft){ const id = store.get("gd.draft","") || S.league?.draft_id; if(id) loadDraft(id); }
    else if(S.draft.meta?.status==="drafting" && !draftTimer) draftTimer = setInterval(()=>loadDraft(S.draft.id, true), 5000);
  } else if(draftTimer){ clearInterval(draftTimer); draftTimer = null; }
}
/* The core rendered before this file loaded; render the new pieces once data is in. */
if(S.me){ renderAll(); }
