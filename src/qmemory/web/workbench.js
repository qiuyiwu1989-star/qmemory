'use strict';
const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const token=$('meta[name=qmemory-session]').content;
const state={project:new URLSearchParams(location.search).get('project')||'',task:'',view:'board',tasks:[],detail:null,offset:0,next:null,source:'',seq:null,request:0,messageRequest:0};
const agent=s=>s==='claude-code'?'Claude Code':s==='codex'?'Codex':s;
const relation=s=>({branch:'执行分支',copy:'关联副本',root:'主来源'}[s]||s);
const status=s=>({active:'已确认 · 有效',proposed:'AI 候选 · 待核对',superseded:'已被替代',archived:'已归档'}[s]||s);
async function api(path,args={}){const r=await fetch('/api/'+path+'?'+new URLSearchParams(args),{headers:{'X-QMemory-Session':token},cache:'no-store'});const data=await r.json();if(!r.ok)throw Error(data.hint||data.error);return data}
function fail(e){$('#notice').textContent='未能读取：'+e.message+'。原始记录未修改。'}
function taskRows(){const q=$('#search').value.toLocaleLowerCase();return state.tasks.filter(t=>t.title.toLocaleLowerCase().includes(q))}
function title(t){return `<strong>${esc(t.title)}</strong><small>${t.agents.map(agent).map(esc).join(' · ')} · ${t.source_count} 份来源${t.branch_count?' · '+t.branch_count+' 个分支':''}</small>`}
function render(){
 $('#tasks').innerHTML=taskRows().map(t=>`<button data-task="${esc(t.id)}" class="${t.id===state.task?'active':''}">${title(t)}</button>`).join('');
 document.querySelectorAll('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===state.view);b.setAttribute('aria-pressed',b.dataset.view===state.view?'true':'false')});
 $('#count').textContent=state.tasks.length+' 项任务';
 const d=state.detail;
 if(state.view==='board'){$('#view').innerHTML='<h1>真实工作，有迹可循</h1><p>沿用现有会话关联，按来源结构分组；不把 Agent 自述完成当成已验收。</p><div class="columns">'+['单来源任务','多来源任务','含执行分支'].map((label,i)=>`<section><h2>${label}</h2>${taskRows().filter(t=>(t.branch_count?2:t.source_count>1?1:0)===i).map(t=>`<button class="card" data-task="${esc(t.id)}">${title(t)}<small>工作状态尚未核实</small></button>`).join('')||'<p>暂无任务</p>'}</section>`).join('')+'</div>';return}
 if(!d){$('#view').innerHTML='<div class="empty">请从左侧选择一个任务。</div>';return}
 const heading='<h1>'+esc(d.title)+'</h1>';
 if(state.view==='story'){
  $('#view').innerHTML=heading+'<p>来源画布 · '+d.source_count+' 份记录 · 尚未生成 AI 故事</p><div class="hint">这里显示真实来源结构，不虚构起因、转折和成果。每份副本与子 Agent 分支均可单独查看。</div><div class="stage">'+d.sources.map(s=>`<button class="card" data-source="${esc(s.id)}"><span class="badge">${esc(relation(s.relation))}</span><h2>${esc(agent(s.agent))}</h2><strong>${esc(s.title)}</strong><small>${s.message_count} 条索引消息 · ${esc(s.id.slice(-8))}</small><small>索引更新 ${esc(s.indexed_at)}</small></button>`).join('')+'</div>';
 }else if(state.view==='map'){
  $('#view').innerHTML=heading+'<p>任务 → 可追溯判断 → 原始依据</p>'+(!d.memory_index_available?'<div class="empty">记忆索引尚不可用；这不代表没有记忆。</div>':!d.memories.length?'<div class="empty">尚无直接引用本任务的记忆。不自动填充示例或推测。</div>':d.memories.map(m=>`<section class="card memory ${esc(m.status)}"><span class="badge">${esc(status(m.status))}</span><h2>${esc(m.subject)}</h2><strong>${esc(m.statement)}</strong><small>成立于 ${esc(m.as_of)} · 私人本地</small><p class="ref">${esc(m.source_ref)}</p><button data-ref="${esc(m.source_ref)}">查看原话依据 →</button></section>`).join(''));
 }else{
  $('#view').innerHTML=heading+'<p>保留每份来源的完整索引顺序，不按相同短句删除多次发生。</p>'+d.sources.map(s=>`<button class="card" data-source="${esc(s.id)}"><strong>${esc(agent(s.agent))} · ${s.message_count} 条索引消息</strong><small>${esc(s.title)} · ${esc(s.relation)}</small></button>`).join('');
 }
}
async function loadProject(){const generation=++state.request;state.messageRequest++;state.project=$('#project').value;state.task='';state.detail=null;state.tasks=[];$('#evidence').hidden=true;render();if(!state.project)return;try{const data=await api('tasks',{project:state.project});if(generation!==state.request)return;state.tasks=data.tasks;render();$('#notice').textContent='真实索引 · 按现有关系归组 · 云端未连接';}catch(e){if(generation===state.request)fail(e)}}
async function selectTask(id){const generation=++state.request;state.messageRequest++;state.task=id;state.detail=null;$('#evidence').hidden=true;render();try{const d=await api('task',{project:state.project,task:id});if(generation!==state.request)return;state.detail=d;if(state.view==='board')state.view='story';render();}catch(e){if(generation===state.request)fail(e)}}
async function evidence(source,offset=0,seq=null){
 const generation=++state.messageRequest;state.source=source;state.seq=seq;
 $('#evidence').hidden=false;
 $('#source').innerHTML=state.detail.sources.map(s=>`<option value="${esc(s.id)}">${esc(agent(s.agent))} · ${esc(relation(s.relation))} · ${esc(s.id.slice(-8))}</option>`).join('');
 $('#source').value=source;$('#messages').textContent='正在读取来源…';$('#page').textContent='';
 $('#prev').disabled=$('#next').disabled=true;
 try{
  const args={project:state.project,task:state.task,source,offset};if(seq)args.sequence=seq;
  const data=await api('messages',args);if(generation!==state.messageRequest)return;
  state.offset=data.offset;state.next=data.next_offset;
  $('#version').textContent='当前索引 · 规则脱敏展示 · 版本 '+data.version;
  $('#messages').innerHTML=data.messages.map(m=>`<article class="${m.sequence===seq?'target':''}"><small>${esc(m.role)} · #${m.sequence} · ${esc(m.occurred_at)}</small><p>${esc(m.text)}</p>${m.truncated?'<small>长消息仅预览前 20,000 字符；未删改原文。</small>':''}<div class="ref">${esc(m.source_ref)}</div></article>`).join('')||'<p>此来源没有索引消息。</p>';
  $('#page').textContent=`${data.total?data.offset+1:0}–${data.offset+data.messages.length} / ${data.total}`;
  $('#prev').disabled=data.offset===0;$('#next').disabled=data.next_offset===null;$('#messages').scrollTop=0;
 }catch(e){if(generation===state.messageRequest){$('#messages').textContent='读取失败，请重新选择来源。';fail(e)}}
}
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.view){state.view=b.dataset.view;render()}if(b.dataset.task)selectTask(b.dataset.task);if(b.dataset.source)evidence(b.dataset.source);if(b.dataset.ref){const match=/^(codex|claude-code):\/\/(.+)#message-(\d+)$/.exec(b.dataset.ref);const s=match&&state.detail.sources.find(s=>s.agent===match[1]&&s.id.replace(/^claude:/,'')===match[2]);if(s)evidence(s.id,0,+match[3]);else $('#notice').textContent='此引用无法在当前任务中定位；没有跨项目查找。'}});
$('#project').onchange=loadProject;$('#search').oninput=render;$('#source').onchange=()=>evidence($('#source').value);$('#close').onclick=()=>{$('#evidence').hidden=true;state.messageRequest++};$('#prev').onclick=()=>evidence(state.source,Math.max(0,state.offset-30));$('#next').onclick=()=>{if(state.next!==null)evidence(state.source,state.next)};
document.addEventListener('keydown',e=>{if(e.key==='Escape')$('#close').click()});
async function init(){
 const generation=++state.request,old=state.project,task=state.task;
 state.messageRequest++;$('#refresh').disabled=true;
 try{
  const data=await api('projects');if(generation!==state.request)return;
  $('#project').innerHTML=data.projects.map(p=>`<option value="${esc(p.id)}">${esc(p.name)} · ${p.identity_kind==='repository'?'仓库':'目录'} · ${p.source_count}</option>`).join('');
  if(data.projects.some(p=>p.id===old))$('#project').value=old;
  const project=$('#project').value;
  await loadProject();
  if(generation+1!==state.request)return;
  if(project===old&&state.tasks.some(t=>t.id===task))await selectTask(task);
  if(!data.projects.length)$('#notice').textContent='还没有已归档项目。请先在 QMemory 桌面端同步。';
 }catch(e){if(generation===state.request)fail(e)}finally{$('#refresh').disabled=false}
}
$('#refresh').onclick=init;init();
