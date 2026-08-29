async function api(url, opts={}){const r=await fetch(url,opts);let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.error||'Ошибка');return d}
async function requireAuth(){const m=await api('/api/me');if(!m.authenticated)location='/login'}
function esc(s=''){return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
async function logout(){await api('/api/logout',{method:'POST'});location='/login'}
