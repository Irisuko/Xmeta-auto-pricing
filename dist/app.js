const $ = (selector) => document.querySelector(selector);
let state = null, csrf = '', filter = 'all', pending = false, toastTimer;
const selected = new Set();
let selectionMode = null;
const batchActive = () => state?.batch && ['queued','running'].includes(state.batch.status);
const visibleItems = () => (state?.items || []).filter(x => filter === 'all' || (filter === 'action' ? x.decision.status === 'action' : x.decision.status === 'protected'));
const followItems = (item) => item.followItems || [{id:item.id,price:item.price,decision:item.decision}];
const actionCount = (item) => followItems(item).filter(x=>x.decision.status==='action').length;
const groupRequest = (item) => ({id:item.id,groupId:item.groupId,price:item.price,target:item.decision.target,
  followCount:item.followCount ?? 1,candidates:followItems(item).map(x=>({id:x.id,price:x.price,target:x.decision.target}))});
const money = (n) => n == null ? '—' : `¥${(n / 100).toFixed(2)}`;
const stageNote = (item) => {
  const actions=followItems(item).filter(x=>x.decision.status==='action');
  if(!actions.some(x=>x.decision.finalTarget != null)) return '';
  const prices=actions.map(x=>x.decision.target), low=Math.min(...prices), high=Math.max(...prices);
  const finalTarget=item.decision.finalTarget ?? item.decision.target;
  return `<span class="drop-note">分次：本次 ${money(low)}${high>low?'–'+money(high):''}，最终 ${money(finalTarget)}</span>`;
};
const timeText = (n) => n ? new Date(n * 1000).toLocaleTimeString('zh-CN', {hour12:false}) : '—';
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function notify(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 6500); }
function toCents(value) { if(!/^\d+(?:\.\d{1,2})?$/.test(value)) throw Error('价格最多保留两位小数'); return Math.round(Number(value) * 100); }
function fillRules() { if(!state) return; $('#interval').value = state.rules.interval; $('#step').value = (state.rules.step/100).toFixed(2); $('#max-drop').value = (state.rules.maxDrop/100).toFixed(2); $('#cooldown').value = state.rules.cooldown; }
async function refresh() { const response = await fetch('/api/state'); if(!response.ok) throw Error('无法连接本机服务'); const result = await response.json(); csrf = result.csrf; state = result; render(); }
async function action(name, data={}) {
  if(pending) throw Error('上一项操作正在处理中，请稍候');
  pending = true; render();
  try {
    const response = await fetch(`/api/${name}`, {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':csrf}, body:JSON.stringify(data)});
    const result = await response.json();
    if(!response.ok) throw Error(result.error || '操作没有完成');
    state = result; render(); return result;
  } finally { pending = false; await refresh().catch(() => {}); render(); }
}
const handle = (fn) => async (...args) => { try { await fn(...args); } catch(error) { notify(error.message); } };
function render() {
  if(!state) return;
  const demo = state.mode === 'demo';
  const locked = pending || state.busy || batchActive();
  if(selectionMode !== state.mode) { selected.clear(); selectionMode = state.mode; }
  for(const id of selected) if(!state.items.some(x=>x.id===id && x.decision.status==='action')) selected.delete(id);
  const active = state.items.filter(x => x.enabled);
  const actions = state.items.filter(x => x.decision.status === 'action');
  const protectedItems = state.items.filter(x => x.decision.status === 'protected');
  $('#stat-total').textContent = active.length;
  $('#stat-undercut').textContent = actions.length;
  $('#stat-protected').textContent = protectedItems.length;
  $('#stat-saving').textContent = money(actions.reduce((total,x) => total + x.decision.drop, 0));
  $('#listing-count').textContent = `${state.items.length} 款 / ${state.listingTotal ?? state.items.length} 件`;
  $('#action-count').textContent = actions.length;
  $('.environment').innerHTML = `<span class="status-dot ${demo?'amber':''}"></span>${demo?'演示环境 · 示例数据':'本机连接 · Xmeta'}`;
  $('.notice').innerHTML = `<span class="notice-icon">i</span><div><strong>${demo?'当前为规则演示':'真实行情 · 跟价件数由你设置'}</strong><span>${escape(state.error || (demo?'示例商品和行情用于验证规则，不会修改 Xmeta 真实售价。':'同款合并显示，默认跟价 1 件，可在每款设置中调整件数。读取时自动开启并设置 80% 底价，定时检查保留设置。'))}</span></div>`;
  $('#connection-button').innerHTML = `${demo?'连接 Xmeta':'账号连接'} <span>↗</span>`;
  $('#start-button').textContent = state.running ? 'Ⅱ 暂停监控' : `▷ 开始${demo?'演示':''}监控`;
  $('#monitor-state').innerHTML = `<span class="status-dot ${state.running?'':'muted'}"></span>${state.busy?'检查中':state.running?'监控中':'已暂停'}`;
  $('#check-button').textContent = state.busy?'正在检查…':'↻ 立即检查';
  $('#last-check').textContent = state.lastCheck ? `最近检查 ${timeText(state.lastCheck)}` : '等待首次检查';
  $('#next-check').textContent = state.running ? `下次检查 ${timeText(state.nextCheck)} · 每 ${state.rules.interval} 秒` : '自动监控已暂停';
  $('#reset-button').textContent = demo?'重置演示':'切换演示';
  const items = visibleItems();
  $('#listings-body').innerHTML = items.length ? items.map((item,i) => {
    const d = item.decision;
    return `<tr class="${selected.has(item.id)?'row-selected':''}"><td class="selection-cell"><input type="checkbox" data-select="${escape(item.id)}" aria-label="选择 ${escape(item.name)} 跟价" ${selected.has(item.id)?'checked':''} ${locked || d.status!=='action'?'disabled':''}></td><td><div class="item-info"><div class="item-art" aria-hidden="true">${String(i+1).padStart(2,'0')}</div><div><strong>${escape(item.name)}</strong><small class="group-quantity" title="从自有售价最低的挂单开始选取，包含已有低价件；库存不足按实际件数">在售 ${item.quantity ?? 1} 件 · 设定 ${item.followCount ?? 1} 件 · 待调 ${actionCount(item)} 件</small><small title="该款当前最低价挂单">${escape(item.serial || item.id)}</small></div></div></td><td class="price">${money(item.price)}</td><td class="price">${money(item.rival)}</td><td class="price floor-price">${money(item.floor)}</td><td class="price target-price">${money(d.target)}${stageNote(item)}${d.drop?`<span class="drop-note">合计降低 ${money(d.drop)}</span>`:''}</td><td><span class="tag ${escape(d.status)}" title="${escape(d.reason)}">${escape(d.label)}</span></td><td><div class="table-buttons">${d.status==='action'?`<button class="apply-button" data-apply="${escape(item.id)}" ${locked?'disabled':''}>${demo?'演示跟价':'一键跟价'}</button>`:''}<button class="text-button" data-edit="${escape(item.id)}" ${locked?'disabled':''}>设置</button></div></td></tr>`;
  }).join('') : `<tr><td colspan="8" class="empty">${state.items.length?'没有符合此筛选的商品':demo?'没有演示商品':'尚未读取到在售商品，请连接 Xmeta 并登录。'}</td></tr>`;
  const selectable = items.filter(x=>x.decision.status==='action');
  const selectedVisible = selectable.filter(x=>selected.has(x.id));
  $('#select-all').checked = selectable.length > 0 && selectedVisible.length === selectable.length;
  $('#select-all').indeterminate = selectedVisible.length > 0 && selectedVisible.length < selectable.length;
  $('#select-all').disabled = !!locked || !selectable.length;
  const chosen = state.items.filter(x=>selected.has(x.id));
  $('#selection-summary').textContent = chosen.length ? `已选 ${chosen.length} 款，待调 ${chosen.reduce((total,x)=>total+actionCount(x),0)} 件 · 合计降价 ${money(chosen.reduce((total,x)=>total+x.decision.drop,0))}` : '勾选可跟价商品，按每款设定的件数执行';
  $('#clear-selection').disabled = !!locked || !chosen.length;
  $('#batch-button').disabled = !!locked || !chosen.length;
  $('#batch-button').textContent = `批量${demo?'演示':''}跟价（${chosen.length} 款）`;
  $('#batch-progress').hidden = !state.batch;
  if(state.batch) {
    const batch = state.batch;
    const labels = {queued:'批量任务已排队',running:'批量跟价进行中',completed:'批量跟价处理完成',failed:'批量跟价已停止',cancelled:'已停止剩余商品'};
    $('#batch-summary').textContent = `${labels[batch.status]} · ${batch.completed}/${batch.total}`;
    $('#batch-hint').textContent = `成功 ${batch.succeeded} 件，跳过 ${batch.skipped} 件，失败 ${batch.failed} 件，未执行 ${batch.items.filter(x=>x.status==='not_run').length} 件。${batchActive()?'复用最近检查的行情，有效期 240 秒；逐件改价并确认结果。停止时会先确认当前一件，再停止后续商品。':''}`;
    $('#cancel-batch').hidden = !batchActive();
    $('#cancel-batch').disabled = pending;
    const rowLabels = {queued:'等待',running:'处理中',succeeded:'成功',skipped:'跳过',failed:'失败',not_run:'未执行'};
    $('#batch-results').innerHTML = batch.items.map(x=>`<li><strong>${escape(x.name)}</strong><span class="batch-result-${escape(x.status)}">${rowLabels[x.status]}</span><small>挂单 ${escape(x.serial || x.id)} · ${money(x.price)} → ${money(x.target)} · ${escape(x.message)}</small></li>`).join('');
  }
  $('#activity-list').innerHTML = state.logs.length ? state.logs.slice(0,30).map(log => `<div class="activity-entry"><span class="log-dot ${log.level==='warning'?'warning':''}"></span><div class="log-content"><strong>${escape(log.title)}</strong><p>${escape(log.detail)}</p></div><time>${timeText(log.time)}</time></div>`).join('') : '<div class="empty">检查结果将在这里显示</div>';
  for(const id of ['check-button','start-button','connection-button','reset-button','login-button','connect-button']) $(`#${id}`).disabled = !!locked;
  document.querySelectorAll('button[type="submit"]').forEach(button => button.disabled = !!locked);
}
$('#check-button').addEventListener('click', handle(async () => { await action('check'); notify('检查完成，建议价已更新'); }));
$('#start-button').addEventListener('click', handle(async () => { await action(state.running?'stop':'start'); }));
$('#rules-form').addEventListener('submit', handle(async event => { event.preventDefault(); await action('rules',{interval:Number($('#interval').value),step:toCents($('#step').value),maxDrop:toCents($('#max-drop').value),cooldown:Number($('#cooldown').value)}); fillRules(); notify('规则已保存，改价仍由你点击执行'); }));
document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => { filter=button.dataset.filter; document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b===button)); render(); }));
$('#select-all').addEventListener('change',event=>{ for(const item of visibleItems().filter(x=>x.decision.status==='action')) { if(event.target.checked) selected.add(item.id); else selected.delete(item.id); } render(); });
$('#listings-body').addEventListener('change',event=>{const checkbox=event.target.closest('[data-select]');if(!checkbox)return;if(checkbox.checked)selected.add(checkbox.dataset.select);else selected.delete(checkbox.dataset.select);render();});
$('#clear-selection').addEventListener('click',()=>{selected.clear();render();});
$('#batch-button').addEventListener('click',handle(async()=>{
  const items=state.items.filter(x=>selected.has(x.id) && x.decision.status==='action').map(groupRequest);
  await action('batch_apply',{mode:state.mode,items}); selected.clear(); render(); notify('批量任务已开始，可在列表上方查看逐件结果');
}));
$('#cancel-batch').addEventListener('click',handle(async()=>{await action('cancel_batch');notify('已停止剩余商品，已提交结果保留');}));
$('#listings-body').addEventListener('click', handle(async event => {
  const edit = event.target.closest('[data-edit]');
  if(edit) { const item = state.items.find(x => x.id === edit.dataset.edit); $('#edit-id').value=item.id; $('#edit-name').textContent=`${item.name} · 在售 ${item.quantity ?? 1} 件`; $('#edit-floor').value=item.floor?(item.floor/100).toFixed(2):''; $('#edit-follow-count').value=item.followCount ?? 1; $('#edit-enabled').checked=item.enabled; $('#pending-label').hidden=!item.pending; $('#pending-message').textContent=`我已在平台核实${item.unassignedPendingIds?.length?'该款及无法归类的旧版':'该款所有'}待确认挂单的最终售价和保证金状态，解除锁定。挂单：${(item.pendingIds || []).join('、')}`; $('#edit-resolve-pending').checked=false; $('#edit-dialog').showModal(); return; }
  const apply = event.target.closest('[data-apply]');
  if(apply) { const item = state.items.find(x=>x.id===apply.dataset.apply); const multiple=(item.followCount ?? 1)>1; await action('apply',groupRequest(item)); notify(multiple?'跟价任务已开始，可在列表上方查看逐件结果':state.mode==='demo'?'演示价格已调整':'已确认该款挂单改价成功'); }
}));
$('#edit-form').addEventListener('submit', handle(async event => { event.preventDefault(); const count=Number($('#edit-follow-count').value); if(!Number.isInteger(count)||count<1||count>100) throw Error('跟价件数请输入 1 至 100 的整数'); await action('item',{id:$('#edit-id').value,floor:toCents($('#edit-floor').value),followCount:count,enabled:$('#edit-enabled').checked,resolvePending:$('#edit-resolve-pending').checked}); $('#edit-dialog').close(); notify('商品设置已保存'); }));
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>document.getElementById(button.dataset.close).close()));
$('#connection-button').addEventListener('click',()=>$('#connection-dialog').showModal());
$('#login-button').addEventListener('click',handle(async()=>{ await action('login'); notify('请在打开的 Xmeta 专用窗口完成登录'); }));
$('#connect-button').addEventListener('click',handle(async()=>{ await action('mode',{mode:'live'}); selected.clear(); $('#connection-dialog').close(); notify('已读取出售列表，可比价商品已开启并设置 80% 底价'); }));
$('#reset-button').addEventListener('click',handle(async()=>{ await action(state.mode==='demo'?'reset':'mode',state.mode==='demo'?{}:{mode:'demo'}); fillRules(); notify('已恢复演示数据，监控已暂停'); }));
$('#export-button').addEventListener('click',()=>{ if(!state)return; const content = state.logs.map(x=>`[${new Date(x.time*1000).toLocaleString('zh-CN')}] [${x.mode==='demo'?'演示':'真实'}] ${x.title} ${x.detail}`).join('\n'); const url=URL.createObjectURL(new Blob(['\ufeff'+content],{type:'text/plain;charset=utf-8'})); const link=document.createElement('a'); link.href=url; link.download=`Xmeta检查记录-${new Date().toISOString().slice(0,10)}.txt`; link.click(); setTimeout(()=>URL.revokeObjectURL(url),1000); });
await refresh().then(fillRules).catch(()=>{ $('#listings-body').innerHTML='<tr><td colspan="8" class="empty">本机服务未连接。请双击项目中的“启动跟价助手.cmd”打开网页。</td></tr>'; notify('请通过启动脚本打开本机工具'); });
setInterval(()=>{ if(!pending) refresh().catch(()=>{ $('.environment').textContent='本机服务已断开'; $('#check-button').disabled=true; $('#start-button').disabled=true; }); },3000);
const context=document.modelContext;
if(context?.registerTool){
  const lifecycle=new AbortController();
  Promise.resolve(context.registerTool({name:'read_xmeta_pricing_status',title:'读取跟价状态',description:'读取当前本机跟价面板的商品和建议，不提交交易。',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true,untrustedContentHint:true},execute:async input=>{if(input&&Object.keys(input).length)throw Error('此工具不接受参数');await refresh();return {mode:state.mode,items:state.items,running:state.running};}},{signal:lifecycle.signal})).catch(()=>{});
  addEventListener('pagehide',()=>lifecycle.abort(),{once:true});
}
