const $ = s => document.querySelector(s);
const fmt = n => new Intl.NumberFormat().format(n);
const rid = p => `${p}-${crypto.randomUUID()}`;
function toast(msg){ const el=$('#toast'); el.textContent=msg; el.classList.add('show'); setTimeout(()=>el.classList.remove('show'),2400); }
async function api(url, opts={}){ const r=await fetch(url,{headers:{'Content-Type':'application/json',...(opts.headers||{})},...opts}); const j=await r.json(); if(!r.ok) throw new Error(j.detail||'Request failed'); return j; }
async function refresh(){ const p=await api('/api/player/1'); $('#gc').textContent=fmt(p.wallets.GC); $('#sc').textContent=fmt(p.wallets.SC); }
async function loadGames(){ const games=await api('/api/games'); $('#games').innerHTML=games.map(g=>`<article class="game card"><div><div class="eyebrow">RTP ${(g.rtp*100).toFixed(1)}% • max ${g.max_multiplier}×</div><h3>${g.name}</h3><p class="meta">${g.description} Version ${g.version}</p></div><div><div class="controls"><select id="cur-${g.key}"><option>GC</option><option>SC</option></select><input id="stake-${g.key}" type="number" min="1" value="1"><button data-play="${g.key}">Play</button></div><div class="result" id="res-${g.key}"></div></div></article>`).join('');
  document.querySelectorAll('[data-play]').forEach(b=>b.onclick=()=>play(b.dataset.play));
}
async function play(game){ try{ const currency=$(`#cur-${game}`).value; const stake=Number($(`#stake-${game}`).value); const j=await api('/api/play',{method:'POST',body:JSON.stringify({request_id:rid('round'),user_id:1,game_key:game,currency,stake})}); $(`#res-${game}`).textContent=j.payout?`WIN +${j.payout} ${currency} (${j.result.multiplier}×)`:`No payout • draw ${j.result.draw}`; await refresh(); }catch(e){toast(e.message)} }
$('#daily').onclick=async()=>{ try{const j=await api('/api/daily-grant/1',{method:'POST'}); toast(j.claimed?'Daily grant credited':'Already claimed today'); await refresh();}catch(e){toast(e.message)} };
document.querySelectorAll('[data-buy]').forEach(b=>b.onclick=async()=>{try{const j=await api('/api/sandbox/purchase',{method:'POST',body:JSON.stringify({request_id:rid('purchase'),user_id:1,package_key:b.dataset.buy})}); toast(`Sandbox package credited: ${fmt(j.server_price.gc)} GC + ${j.server_price.sc} SC`); await refresh();}catch(e){toast(e.message)}});
$('#redeem').onclick=async()=>{try{const amount=Number($('#redeemAmount').value); const j=await api('/api/redemptions',{method:'POST',body:JSON.stringify({request_id:rid('redeem'),user_id:1,amount})}); toast(`Reserved ${amount} SC • ${j.status}`); await refresh();}catch(e){toast(e.message)}};
$('#treasury').onclick=async()=>{try{const j=await api('/api/admin/treasury',{headers:{'X-Admin-Key':'dev-admin-key'}}); $('#treasuryOut').textContent=JSON.stringify(j,null,2);}catch(e){toast(e.message)}};
loadGames(); refresh();
