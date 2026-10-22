import React, {Suspense,lazy,useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import './style.css';
const TerminalPanel=lazy(()=>import('./TerminalPanel').then(module=>({default:module.TerminalPanel})));
import {ProjectPanel} from './ProjectPanel';
import {GlobalInputHistory} from './GlobalInputHistory';
import {ReflectionPanel} from './ReflectionPanel';
import {CoveragePanel} from './CoveragePanel';
import {ExpertPanel} from './ExpertPanel';
import {EvaluationPanel} from './EvaluationPanel';
import {HistoryPanel} from './HistoryPanel';
import {ClosurePanel} from './ClosurePanel';
import {RecoveryPanel} from './RecoveryPanel';
import {AcceptancePanel} from './AcceptancePanel';
import {PMDialogue} from './PMDialogue';
import {readResponse} from './api';

type Session = {id:string; agent:string; state:string; owner:string; epoch:number};
type Task = {id:string; description:string; state:string; attempt:number; agent?:string; reason?:string};
type Run = {id:string; task_id:string; state:string; provider?:string; session_id?:string; phase?:string; head_sha?:string; reason?:string; artifact_sha256?:string; conversation?:{instruction:string;events:Output[]}};
type AgentQuestion = {id:string; state:string; answered_by?:string; reason?:string; answers?:string[][]; questions:{id:string;question:string;options:{label:string;description:string}[];multiple:boolean;custom:boolean;secret:boolean}[]};
type Batch = {id:string;state:string;reason?:string;stop_category?:string;owner_next_action?:string;candidate_sha?:string;completion_inventory?:{outside_scope_remaining:string[]}};
type Approval = {id:string; state:string; session_id:string; revision:number; operation_hash:string};
type AgentHealth = {agent:string;state:string;failures:number;reason?:string};
type Snapshot = {tasks:Task[]; sessions:Session[]; approvals:Approval[]; latest_seq:number; paused:boolean; live_ids:string[]; runs:Run[]; questions:AgentQuestion[]; batches:Batch[];agent_health?:AgentHealth[];totals?:Record<string,number>;task_page?:{offset:number;next_offset:number|null;total:number};task_counts?:Record<string,number>;pm_progress?:{percent:number;completed_weight:number;total_weight:number;remaining_equivalent_tasks:number;basis:string};payloads_omitted?:number};
type Output = {seq:number; session_id:string; type:string; text?:string; code?:string; activity?:string; detail?:string; is_error?:boolean; state?:string};
type Project = {id:string; name:string; root:string};
type Proposal = {id:string; tasks:{id:string; description:string; acceptance:string[]; dependencies:string[]}[]};
const empty: Snapshot = {tasks:[],sessions:[],approvals:[],latest_seq:0,paused:false,live_ids:[],runs:[],questions:[],batches:[]};
const labels:Record<string,string> = {todo:'待执行',leased:'已领取',verifying:'验证中',done:'已验证',blocked:'已阻塞',cancelled:'已取消',ready:'就绪',reconciling:'等待对账',completed:'回合完成',failed:'执行失败',acknowledged:'已接收'};
// Cards are an operational overview. The task console is the only place that
// renders the captured, redacted payload verbatim.
function eventSummary(event:Output) { if(event.type==='text')return '\u6536\u5230 Agent \u56de\u590d'; if(event.type==='receipt')return '\u5df2\u63a5\u6536 PM \u6307\u4ee4'; if(event.type==='completed')return '\u56de\u5408\u5b8c\u6210'; if(event.type==='tool_result')return (event.is_error?'\u5de5\u5177\u6b65\u9aa4\u5931\u8d25':'\u5de5\u5177\u6b65\u9aa4\u5df2\u5b8c\u6210')+(event.activity?' \u00b7 '+event.activity:''); return event.code||event.type; }
function eventLabel(event:Output) { return event.type==='text'?(event.text||''):event.type==='tool_result'?eventSummary(event)+(event.detail?'\n'+event.detail:''):eventSummary(event); }

function App() {
  const [token,setToken] = useState('');
  const [draftToken,setDraftToken] = useState('');
  const [project,setProject] = useState<Project|null>(null);
  const [snapshot,setSnapshot] = useState<Snapshot>(empty);
  const [taskOffset,setTaskOffset]=useState(0);
  const [terminalVisible,setTerminalVisible]=useState(false);
  const [output,setOutput] = useState<Output[]>([]);
  const [error,setError] = useState('');
  const [connected,setConnected] = useState(false);
  const [busy,setBusy] = useState('');
  const [executionAgent,setExecutionAgent] = useState('claude');
  const [taskDescription,setTaskDescription] = useState('');
  const [taskAcceptance,setTaskAcceptance] = useState('');
  const [goal,setGoal] = useState('');
  const [criteria,setCriteria] = useState('');
  const [proposal,setProposal] = useState<Proposal|null>(null);
  const [approval,setApproval] = useState<(Approval & {operation:unknown;live:boolean})|null>(null);
  const cursor = useRef(0);
  const selectedId = useRef('');
  const generation=useRef(0),refreshSequence=useRef(0);
  const prefix = project ? `/api/v1/projects/${project.id}` : '';

  async function api(path:string, body?:unknown, operationKey?:string) {
    const deleting=typeof body==='object'&&body!==null&&('_method' in body)&&(body as {_method?:string})._method==='DELETE';
    const payload=deleting ? Object.fromEntries(Object.entries(body as Record<string,unknown>).filter(([key])=>key!=='_method')) : body;
    const init={method:deleting ? 'DELETE' : body === undefined ? 'GET':'POST', headers:{Authorization:`Bearer ${token}`,
      ...(body === undefined ? {} : {'Content-Type':'application/json','Idempotency-Key':operationKey||crypto.randomUUID()})},
      body:payload === undefined ? undefined:JSON.stringify(payload)};
    let response:Response|undefined;
    for(let attempt=0;attempt<8;attempt++){
      const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
      try {response=await fetch(path,{...init,signal:controller.signal});break} catch {if(attempt<7)await new Promise(resolve=>setTimeout(resolve,Math.min(4000,500*(attempt+1))))}
      finally {clearTimeout(timer)}
    }
    if(!response)throw new Error('本机服务尚未恢复连接；PM 已保留检查点，将自动继续重连。');
    return readResponse(response);
  }
  async function refresh() {
    if (!project) return;
    const version=generation.current,sequence=++refreshSequence.current;
    let state:Snapshot,messages:any;
    try {[state,messages]=await Promise.all([api(prefix+'/snapshot'+(taskOffset?'?task_offset='+taskOffset:'')),api(prefix+`/output?after=${cursor.current}`)]);}catch(e){
      if(version!==generation.current||sequence!==refreshSequence.current)return;
      setConnected(false);throw e;
    }
    if(version!==generation.current||sequence!==refreshSequence.current)return;
    setConnected(true);
    setError(old=>old.startsWith('本机服务尚未恢复连接')?'':old);
    setSnapshot(state);
    if (messages.gap) setError('部分实时输出已超出保留窗口；任务和命令记录仍可查看。');
    cursor.current = messages.latest_seq;
    setOutput(old=>Array.from(new Map([...old,...messages.events].map(item=>[item.seq,item])).values()).sort((a,b)=>a.seq-b.seq).slice(-2000));
  }
  useEffect(()=>{
    if (!token) return;
    let alive=true;
    const connect=async()=>{
      let attempt=0;
      while(alive){
        try {const p=await api('/api/v1/project');if(alive){setProject(p);setError('')}return}
        catch(e){
          if(!alive)return;
          const message=e instanceof Error?e.message:String(e);
          if(message.startsWith('本机服务尚未恢复连接')){
            setError(message);await new Promise(resolve=>setTimeout(resolve,Math.min(10000,1000*2**Math.min(attempt++,4))));continue;
          }
          setError(message);setToken('');return;
        }
      }
    };
    void connect();
    return ()=>{alive=false};
  },[token]);
  useEffect(()=>{
    if (!project) return;
    let active=true;
    const poll=async()=>{try{await refresh()}catch(e){if(active)setError(e instanceof Error&&e.message.startsWith('本机服务尚未恢复连接')?e.message:String(e))}finally{if(active)timer=setTimeout(poll,1000)}};
    let timer:ReturnType<typeof setTimeout>;
    void poll();
    return ()=>{active=false;clearTimeout(timer)};
  },[project,token,taskOffset]);
  async function action(name:string, run:()=>Promise<unknown>) {
    const version=generation.current;setBusy(name);setError('');
    try {await run(); if(version===generation.current)await refresh(); return true} catch(e) {if(version===generation.current)setError(e instanceof Error ? e.message:String(e)); return false} finally {if(version===generation.current)setBusy('')}
  }
  function selectProject(next:Project) {
    setTaskOffset(0);
    setTerminalVisible(false);
    if(next.id!==project?.id){generation.current++;selectedId.current=next.id;cursor.current=0;setConnected(false);setBusy('');setSnapshot(empty);setOutput([]);setProposal(null);setApproval(null);setGoal('');setCriteria('');setTaskDescription('');setTaskAcceptance('');setError('')}
    setProject(next);
  }
  if (!project) return <main className="login"><div className="mark">A</div><p className="eyebrow">LOCAL DEVELOPMENT WORKSPACE</p><h1>开发协作工作台</h1><p>把指令、执行与验证放在同一个工作区。</p><section>
    <h2>连接本机服务</h2><label>访问令牌<input type="password" value={draftToken} onChange={e=>setDraftToken(e.target.value)} autoComplete="off" placeholder="输入本机工作台访问令牌"/></label>
    <label className="file">或选择 owner-token.txt<input type="file" accept=".txt" onChange={async e=>{const file=e.target.files?.[0];if(file)setDraftToken((await file.text()).trim())}}/></label>
    <button className="primary" disabled={!draftToken.trim()} onClick={()=>{setToken(draftToken.trim());setDraftToken('')}}>进入工作台 →</button><p className="muted">令牌文件位于项目的 .local/workbench 目录。请使用工作台令牌，不要输入模型 API key。</p>{error&&<p role="alert" className="error">{error}</p>}</section></main>;
  const pending = snapshot.approvals.filter(a=>a.state==='pending');
  const batch=[...(snapshot.batches||[])].reverse()[0];
  // Reconciliation is an active PM recovery loop, not permission to start a
  // competing filtered batch whose dependency closure is incomplete.
  const batchActive=!!batch&&!['verified_candidate','rework_required','failed'].includes(batch.state);
  const criteriaItems=criteria.split('\n').map(item=>item.trim()).filter(Boolean);
  // The owner supplies product outcomes. PM creates and maintains executable
  // verification; the form must never ask the owner for command names.
  const existingTodos=snapshot.tasks.filter(task=>task.state==='todo');
  const activeRuns=snapshot.tasks.filter(task=>['leased','verifying'].includes(task.state)).map(task=>({task,run:[...(snapshot.runs||[])].reverse().find(run=>run.task_id===task.id)}));
  const hasNewDelegation=!!goal.trim()&&!!criteriaItems.length;
  const delegationDisabled=!!busy||snapshot.paused||batchActive||(!hasNewDelegation&&!existingTodos.length);
  return <div className="shell"><header><div className="brand"><span className="mark">A</span><div><strong>开发协作</strong><small>本地工作台 · {project.name}</small></div></div><div className="header-actions"><span className={'dot '+(!connected||snapshot.paused?'amber':'')}/><span>{!connected?'服务未连接或正在重连':snapshot.paused?'派发已暂停':'服务已连接'}</span><button disabled={!!busy} onClick={()=>action('pause',()=>api(prefix+'/pause',{paused:!snapshot.paused}))}>{snapshot.paused?'继续派发':'暂停派发'}</button></div></header><ProjectPanel project={project} api={api} select={selectProject} disabled={!!busy}/><GlobalInputHistory api={api} usePlan={(savedGoal,savedAcceptance)=>{setGoal(savedGoal);setCriteria(savedAcceptance.join('\n'))}}/><PMDialogue key={'pm-dialogue:'+project.id} prefix={prefix} api={api}/>
    <div className="overview"><div><p className="eyebrow">PROJECT WORKSPACE</p><h1>让每一步执行都有据可查。</h1><p className="muted">{project.root}</p></div><div className="metrics"><div><b>{snapshot.pm_progress?.percent??0}<i>%</i></b><span title={snapshot.pm_progress?.basis}>PM 预估总体进度</span><progress aria-label="PM 预估总体进度" value={snapshot.pm_progress?.percent??0} max="100"/></div><div><b>{snapshot.task_counts?.done??snapshot.tasks.filter(t=>t.state==='done').length}<i> / {snapshot.totals?.tasks??snapshot.tasks.length}</i></b><span>任务已验证</span></div><div><b>{snapshot.pm_progress?.remaining_equivalent_tasks??0}</b><span>预估剩余等价任务</span></div><div><b>{snapshot.live_ids.length}</b><span>在线会话</span></div><div><b>{pending.length}</b><span>本页等待审批</span></div></div></div>
    {error&&<div role="alert" className="banner"><span>{error}</span><button onClick={()=>setError('')}>关闭</button></div>}
    {!!snapshot.payloads_omitted&&<p role="status">有 {snapshot.payloads_omitted} 条历史内容过大，页面仅显示安全摘要；完整记录仍保留在本地账本，任务操作不受影响。</p>}
    <details className="panel planning"><summary>与 PM 制定任务计划</summary><p className="muted">说明项目目标和验收条件。PM 生成草案，经你采用后加入任务清单。</p><form onSubmit={e=>{e.preventDefault();void action('planning',async()=>setProposal(await api(prefix+'/pm/proposals',{goal,acceptance:criteria.split('\n').filter(x=>x.trim())})))}}><div className="plan-inputs"><label>项目目标<textarea required value={goal} onChange={e=>setGoal(e.target.value)} placeholder="希望交付什么，范围和限制是什么？"/></label><label>项目验收条件（每行一条）<textarea required value={criteria} onChange={e=>setCriteria(e.target.value)} placeholder="如何判断工作完成？"/></label></div><GlobalPlanSelect api={api} usePlan={(savedGoal,savedAcceptance)=>{setGoal(savedGoal);setCriteria(savedAcceptance.join('\n'))}}/><button className="primary" disabled={!!busy||snapshot.paused}>{busy==='planning'?'PM 正在制定计划…':'生成任务草案'}</button></form>{proposal&&<div className="plan-draft"><h2>任务草案 · {proposal.tasks.length} 项</h2>{proposal.tasks.map(t=><article key={t.id}><strong>{t.description}</strong><p>{t.acceptance.join('；')}</p><small>{t.dependencies.length?`依赖 ${t.dependencies.length} 项任务`:'可独立开始'}</small></article>)}<button disabled={!!busy} className="primary" onClick={()=>action('adopt',async()=>{await api(prefix+'/pm/proposals/'+proposal.id+'/adopt',{});setProposal(null)})}>采用计划</button></div>}</details>
    <div className="panel"><button disabled={delegationDisabled} onClick={()=>void action('delegating',async()=>{if(hasNewDelegation){await api(prefix+'/pm/delegate',{goal,acceptance:criteriaItems,agent:executionAgent==='mixed'?'claude':executionAgent,agents:executionAgent==='mixed'?['codex','opencode','claude']:null,max_concurrency:executionAgent==='mixed'?3:1});setProposal(null)}else await api(prefix+'/batches',{agent:executionAgent==='mixed'?'claude':executionAgent,agents:executionAgent==='mixed'?['codex','opencode','claude']:null,max_concurrency:executionAgent==='mixed'?3:1,task_ids:existingTodos.map(task=>task.id)})})}>{busy==='delegating'?'PM 正在启动…':hasNewDelegation?'委托 PM 规划并执行':'委托 PM 执行现有待办'}</button><p className="muted">填写上方目标和业务验收标准时，PM 会重新规划后执行；未填写时，PM 直接执行当前 {existingTodos.length} 项待办。PM 自动维护独立验证、按依赖开发、盘点复审并验收；每任务最多两次尝试。</p>{hasNewDelegation&&<p className="muted">本次业务验收标准：{criteriaItems.join('、')}。</p>}</div>
    <label className="execution-agent">执行 Agent <select aria-label="执行 Agent" value={executionAgent} disabled={!!busy||batchActive} onChange={e=>setExecutionAgent(e.target.value)}><option value="claude">Claude</option><option value="codex">Codex</option><option value="opencode">OpenCode</option><option value="mixed">三 Agent 并发</option></select></label><BatchControls batch={batch} pageOnly={(snapshot.totals?.tasks??0)>snapshot.tasks.length} disabled={!!busy||snapshot.paused||batchActive||!snapshot.tasks.some(t=>t.state==='todo')} start={()=>action('batch',()=>api(prefix+'/batches',{agent:executionAgent==='mixed'?'claude':executionAgent,agents:executionAgent==='mixed'?['codex','opencode','claude']:null,max_concurrency:executionAgent==='mixed'?3:1,task_ids:snapshot.tasks.filter(t=>t.state==='todo').map(t=>t.id)}))}/>    {activeRuns.length>0&&<section className="execution-now" aria-live="polite"><div><p className="eyebrow">LIVE EXECUTION</p><h2>正在执行 {activeRuns.length} 项任务</h2></div><div className="execution-list">{activeRuns.map(({task,run})=><article key={task.id}><span className="pulse"/><div><strong>{task.description}</strong><small>{run?.provider||task.agent||'PM'} · {run?.phase==='inventory'?'盘点剩余工作':run?.phase==='implementation'?'实现中':run?.phase==='verification'?'验收中':'执行中'}</small>{run?.reason&&<em>{run.reason}</em>}<TaskConsole run={run} live={output.filter(event=>event.session_id===run?.session_id)}/></div></article>)}</div></section>}
<div className="workspace"><TaskList tasks={snapshot.tasks} runs={snapshot.runs||[]} executionAgent={executionAgent} disabled={!!busy||snapshot.paused||batchActive} createDisabled={!!busy} taskDescription={taskDescription} setTaskDescription={setTaskDescription} taskAcceptance={taskAcceptance} setTaskAcceptance={setTaskAcceptance} startTask={(task,run,agent)=>action('run',()=>api(prefix+'/tasks/'+task.id+'/runs',{agent,...(run?.state==='rework_required'?{base_sha:run.head_sha}:{})}))} createTask={()=>action('task',async()=>{await api(prefix+'/tasks',{id:'task_'+crypto.randomUUID().replaceAll('-',''),description:taskDescription,acceptance:taskAcceptance.split('\n').filter(x=>x.trim())});setTaskDescription('');setTaskAcceptance('')})} diagnostics={()=>action('diagnostics',async()=>{const data=await api(prefix+'/diagnostics');const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='diagnostics.json';a.click();URL.revokeObjectURL(url)})}/>
    <section className="agents" aria-label="Agent sessions">{['codex','opencode','claude'].map((agent,index)=>{
      const activeSessions=[...snapshot.sessions].reverse().filter(session=>session.agent===agent&&snapshot.live_ids.includes(session.id));
      const latest=[...snapshot.sessions].reverse().find(session=>session.agent===agent);
      const cards=activeSessions.length?activeSessions:(latest?[{...latest,state:latest.state==='ready'?'offline':latest.state}]:[undefined]);
      const health=snapshot.agent_health?.find(item=>item.agent===agent);
      return <div className="agent-stack" key={project.id+agent}>{cards.map((session,cardIndex)=>{
        const run=session?[...snapshot.runs].reverse().find(item=>item.session_id===session.id):undefined;
        const task=run?snapshot.tasks.find(item=>item.id===run.task_id):undefined;
        return <AgentCard key={session?.id||agent} agent={agent} index={index} health={health} task={task} session={session} messages={output.filter(item=>item.session_id===session?.id)} busy={!!busy} paused={snapshot.paused}
          start={()=>action(agent,()=>api(prefix+'/sessions',{agent,...(session?{resume_from:session.id}:{})}))}
          send={text=>action('send',()=>api(prefix+`/sessions/${session!.id}/commands`,{epoch:session!.epoch,text}))}
          takeover={()=>action('takeover',()=>api(prefix+`/sessions/${session!.id}/takeover`,{expected_epoch:session!.epoch,owner:'human'}))}
          interrupt={()=>action('interrupt',()=>api(prefix+`/sessions/${session!.id}/interrupt`,{}))}/>;
      })}</div>;
    })}</section><aside className="panel approvals"><div className="section-title"><h2>审批与证据</h2><span>{pending.length}</span></div>{(snapshot.questions||[]).filter(q=>q.state!=='stale').map(q=><QuestionCard key={q.id} question={q} disabled={!!busy} answer={answers=>action('answer',()=>api(prefix+'/questions/'+q.id+'/answer',{answers}))}/>)}{pending.length===0?<p className="empty">暂无待处理审批。需要授权的操作会显示在这里。</p>:pending.map(a=><button className="approval-item" key={a.id} onClick={()=>action('approval',async()=>setApproval(await api(prefix+'/approvals/'+a.id)))}>工具操作待审批 <small>{a.session_id.slice(-8)}</small>查看操作 →</button>)}<div className="notice"><strong>验收以独立验证为准</strong><p>Agent 回合完成后，任务仍需验证。等待对账的会话不会自动重发输入。</p></div><p className="muted footnote">事件游标 #{snapshot.latest_seq}<br/>会话公开输出仅保留于本次服务内存。</p></aside></div>
    {approval&&<div className="modal-backdrop"><section role="dialog" aria-modal="true" aria-label="审批工具操作" className="modal"><h2>审批工具操作</h2><p>请核对目标文件、操作内容和范围。</p><pre>{JSON.stringify(approval.operation,null,2)}</pre><div className="modal-actions"><button onClick={()=>setApproval(null)}>关闭</button>{['denied','approved'].map(d=><button key={d} disabled={!approval.live||!!busy} className={d==='approved'?'primary':''} onClick={()=>action('decision',async()=>{await api(prefix+'/approvals/'+approval.id+'/decision',{decision:d,operation_hash:approval.operation_hash,expected_revision:approval.revision});setApproval(null)})}>{d==='approved'?'批准本次操作':'拒绝'}</button>)}</div></section></div>}
    <ReflectionPanel key={'reflection:'+project.id} prefix={prefix} api={api}/>
    <AcceptancePanel key={'acceptance:'+project.id} prefix={prefix} api={api}/>
    <CoveragePanel key={'coverage:'+project.id} prefix={prefix} api={api}/>
    {snapshot.task_page&&snapshot.task_page.total>100&&<div className="panel"><p>任务分页：共 {snapshot.task_page.total} 项，当前从第 {snapshot.task_page.offset+1} 项开始。批次执行仅针对本页显示的待办。</p><button disabled={!!busy||taskOffset===0} onClick={()=>{generation.current++;setTaskOffset(Math.max(0,taskOffset-100))}}>上一页任务</button><button disabled={!!busy||snapshot.task_page.next_offset===null} onClick={()=>{generation.current++;setTaskOffset(snapshot.task_page!.next_offset!)}}>下一页任务</button></div>}
    <HistoryPanel key={'history:'+project.id} prefix={prefix} api={api}/>
    <ExpertPanel key={'experts:'+project.id} prefix={prefix} api={api} batches={snapshot.batches}/>
    <EvaluationPanel key={'evaluation:'+project.id} prefix={prefix} api={api}/>
    <RecoveryPanel key={'recovery:'+project.id} prefix={prefix} api={api} runs={snapshot.runs} tasks={snapshot.tasks}/>
    <ClosurePanel key={'closure:'+project.id} prefix={prefix} api={api}/>
    {terminalVisible?<Suspense fallback={<p>正在加载终端…</p>}><TerminalPanel key={'terminal:'+project.id} prefix={prefix} token={token} paused={snapshot.paused}/></Suspense>:<section className="panel"><button onClick={()=>setTerminalVisible(true)}>打开本机终端</button><p>打开时加载终端组件并连接输出。</p></section>}
  </div>;
}

function GlobalPlanSelect({api,usePlan}:{api:(path:string,body?:unknown)=>Promise<any>;usePlan:(goal:string,acceptance:string[])=>void}) {
  const [plans,setPlans]=useState<{id:string;content:{goal:string;acceptance?:string[]}}[]>([]);
  useEffect(()=>{let active=true;api('/api/v1/input-history?limit=50').then(result=>{if(active)setPlans(result.items.filter((item:any)=>typeof item.content?.goal==='string'))}).catch(()=>{});return()=>{active=false}},[]);
  return <label>复用历史目标<select aria-label="复用历史目标" defaultValue="" onChange={event=>{const plan=plans.find(item=>item.id===event.target.value);if(plan)usePlan(plan.content.goal,Array.isArray(plan.content.acceptance)?plan.content.acceptance:[])}}><option value="">选择历史输入…</option>{plans.map(plan=><option key={plan.id} value={plan.id}>{plan.content.goal.slice(0,80)}</option>)}</select></label>
}

function BatchControls({batch,disabled,start,pageOnly=false}:{batch?:Batch;disabled:boolean;start:()=>unknown;pageOnly?:boolean}) {
  const status:Record<string,string>={queued:'计划已排队',running:'正在按依赖执行计划',paused:'计划已暂停',integrating:'正在验证集成结果',verified_candidate:'集成候选已验证，尚未发布',rework_required:'需要返工',reconciling:'需要对账',failed:'执行停止'};
  return <div className="batch-controls"><button disabled={disabled} className="primary" onClick={start}>{pageOnly?'执行本页待办':'执行全部待办'}</button><span>{batch?status[batch.state]||batch.state:'按依赖推进，完成声明后盘点、复审并独立验证；默认最多尝试两次。'}</span>{batch?.reason&&<p role="alert">暂停原因：{batch.reason}。{batch.owner_next_action||'PM 将保存检查点并继续诊断。'}</p>}{batch?.candidate_sha&&<code>{batch.candidate_sha.slice(0,12)}</code>}{!!batch?.completion_inventory?.outside_scope_remaining.length&&<span>本批次之外仍有 {batch.completion_inventory.outside_scope_remaining.length} 项未完成。</span>}</div>;
}

function QuestionCard({question,disabled,answer}:{question:AgentQuestion;disabled:boolean;answer:(values:string[][])=>Promise<unknown>}) {
  const [selected,setSelected]=useState<string[][]>(question.questions.map(()=>[]));
  const [custom,setCustom]=useState<string[]>(question.questions.map(()=>''));
  const waiting=['pending_pm','awaiting_owner'].includes(question.state);
  return <section className="question-card"><h3>{question.state==='pending_pm'?'PM 正在判断':waiting?'需要你的决定':question.answered_by==='pm'?'PM 已回答':'问题处理记录'}</h3>{question.reason&&<p>{question.reason}</p>}{question.questions.map((q,i)=><fieldset key={q.id} disabled={disabled||!waiting||q.secret}><legend>{q.question}</legend>{waiting?q.options.map(o=><label className="choice" key={o.label}><input type={q.multiple?'checkbox':'radio'} name={question.id+'-'+i} checked={selected[i].includes(o.label)} onChange={e=>{setCustom(old=>old.map((v,n)=>n===i?'':v));setSelected(old=>old.map((v,n)=>n===i?(q.multiple?(e.target.checked?[...v,o.label]:v.filter(x=>x!==o.label)):[o.label]):v))}}/>{o.label}<small>{o.description}</small></label>):<p>{question.answers?.[i]?.join('；')||question.state}</p>}{waiting&&q.custom&&<label>填写自己的想法<textarea value={custom[i]} onChange={e=>{setCustom(old=>old.map((v,n)=>n===i?e.target.value:v));setSelected(old=>old.map((v,n)=>n===i?[]:v))}}/></label>}{q.secret&&<p>凭据问题需在 Agent 原生界面处理。</p>}</fieldset>)}{waiting&&<button disabled={disabled||question.questions.some((q,i)=>q.secret||(!custom[i].trim()&&!selected[i].length))} onClick={()=>answer(selected.map((v,i)=>custom[i].trim()?[custom[i].trim()]:v))}>提交我的选择</button>}</section>;
}

type TaskListProps={tasks:Task[];runs:Run[];executionAgent:string;disabled:boolean;createDisabled:boolean;taskDescription:string;setTaskDescription:(value:string)=>void;taskAcceptance:string;setTaskAcceptance:(value:string)=>void;startTask:(task:Task,run:Run|undefined,agent:string)=>unknown;createTask:()=>unknown;diagnostics:()=>unknown};
function TaskList(props:TaskListProps) {
  const [filter,setFilter]=useState('all');
  const [grouped,setGrouped]=useState(true);
  const bucket=(task:Task)=>task.state==='todo'?'pending':(['leased','verifying'].includes(task.state)?'running':task.state==='done'?'completed':'attention');
  const titles:Record<string,string>={pending:'待运行',running:'正在运行',completed:'已完成',attention:'需处理'};
  const visible=props.tasks.filter(task=>filter==='all'||bucket(task)===filter);
  const groups=grouped?['running','pending','attention','completed'].map(key=>[key,visible.filter(task=>bucket(task)===key)] as const).filter(([,items])=>items.length):[['all',visible] as const];
  const render=(task:Task)=>{const run=[...props.runs].reverse().find(item=>item.task_id===task.id);const taskAgent=props.executionAgent==='mixed'?(['codex','opencode','claude'][props.tasks.filter(item=>item.state==='todo').findIndex(item=>item.id===task.id)%3]||'claude'):props.executionAgent;return <TaskItem key={task.id} agent={taskAgent} task={task} run={run} disabled={props.disabled} start={()=>props.startTask(task,run,taskAgent)}/>};
  return <aside className="panel tasks"><details open className="task-list-details"><summary><span className="section-title"><h2>任务清单</h2><span>{props.tasks.length}</span></span></summary><div className="task-controls"><label>显示<select aria-label="任务状态筛选" value={filter} onChange={event=>setFilter(event.target.value)}><option value="all">全部状态</option><option value="running">正在运行</option><option value="pending">待运行</option><option value="completed">已完成</option><option value="attention">需处理</option></select></label><label><input type="checkbox" checked={grouped} onChange={event=>setGrouped(event.target.checked)}/> 按状态分组</label></div><div className="task-scroll">{visible.length===0?<p className="empty">此状态下暂无任务。</p>:groups.map(([key,items])=><section className="task-group" key={key}>{grouped&&<h3>{titles[key]} <small>{items.length}</small></h3>}{items.map(render)}</section>)}</div></details><details><summary>＋ 新建任务</summary><form onSubmit={event=>{event.preventDefault();void props.createTask()}}><label>目标<textarea required value={props.taskDescription} onChange={event=>props.setTaskDescription(event.target.value)}/></label><label>验收条件（每行一条）<textarea required value={props.taskAcceptance} onChange={event=>props.setTaskAcceptance(event.target.value)}/></label><button className="primary" disabled={props.createDisabled}>创建任务</button></form></details><button className="export" onClick={props.diagnostics}>导出诊断记录 ↗</button></aside>;
}

function TaskItem({task,run,disabled,start,agent}:{agent:string;task:Task;run?:Run;disabled:boolean;start:()=>unknown}) {
  const running=!!run&&!['done','rework_required','reconciling','reconciled','failed'].includes(run.state);
  const states:Record<string,string>={preparing:'准备工作树',starting:'启动 Agent',running:'执行中 · 可在会话卡查看',reviewing:run?.phase==='inventory'?'盘点剩余工作':'架构、测试与体验多视角复审',committing:'保存修改',verifying:'独立验证中',done:'验收通过',rework_required:'检查未通过，可继续返工',reconciling:'需要对账，已停止自动执行',failed:'未能启动'};
  return <article className="task"><strong title={task.id}>{task.description}</strong><span className={'status '+task.state}>{labels[task.state]||task.state}</span><small>尝试 {task.attempt}</small>{run&&<p className="run-status">{states[run.state]||run.state}{run.reason&&<small>{run.reason}</small>}</p>}{run?.artifact_sha256&&<details><summary>验证证据</summary><small className="evidence">SHA-256: {run.artifact_sha256}</small></details>}{task.state==='todo'&&<button disabled={disabled||running} onClick={start}>{run?.state==='rework_required'?'继续返工':`交给 ${{claude:'Claude',codex:'Codex',opencode:'OpenCode'}[agent]} 执行`}</button>}</article>;
}

function TaskConsole({run,live}:{run?:Run;live:Output[]}) {
  const events=run?.conversation?.events?.length?run.conversation.events:live;
  return <details className="task-console" open><summary>{'\u5b9e\u65f6\u6267\u884c\u63a7\u5236\u53f0'}</summary><div className="console-screen" role="log" aria-live="polite">{run?.conversation?.instruction&&<pre className="console-line input"><b>PM $</b> {run.conversation.instruction}</pre>}{events.map((event,index)=><pre className={'console-line '+event.type} key={`${event.seq}-${index}`}><b>{event.type==='text'?'Agent >':event.type==='tool_result'?'Tool  >':event.type==='error'?'Error >':'System>'}</b> {eventLabel(event)}</pre>)}{!run?.conversation?.instruction&&!events.length&&<pre className="console-line">{'\u6b63\u5728\u7b49\u5f85\u4f1a\u8bdd\u8f93\u51fa\u2026'}</pre>}</div></details>;
}

function AgentCard({agent,index,health,task,session,messages,busy,paused,start,send,takeover,interrupt}:{agent:string;index:number;health?:AgentHealth;task?:Task;session?:Session;messages:Output[];busy:boolean;paused:boolean;start:()=>unknown;send:(text:string)=>Promise<unknown>;takeover:()=>unknown;interrupt:()=>unknown}) {
  const [text,setText]=useState('');
  const [expanded,setExpanded]=useState(false);
  const feed=useRef<HTMLDivElement>(null);
  const offline=!!session&&["closed","offline","reconciling"].includes(session.state);
  useEffect(()=>{if(feed.current)feed.current.scrollTop=feed.current.scrollHeight},[messages.length]);
  const emptyMessage=offline?'旧会话已断开；PM 在派发任务时会创建新的受管会话。':'会话已连接，等待第一条指令。';
  return <article className={'agent-card '+(expanded?'expanded':'')}><div className="agent-title"><span className={'agent-icon color-'+index}>{agent.slice(0,1).toUpperCase()}</span><div><h2>{agent==='codex'?'Codex':agent==='claude'?'Claude':'OpenCode'}</h2>{task&&<small className="task-session" title={task.description}>{task.description}</small>}<small>{health?.state==='unavailable'?`自动重连中 · 第 ${health.failures} 次失败`:(session?`${labels[session.state]||session.state} · ${session.owner==='human'?'人工输入':'PM 输入'}`:'尚未启动')}</small></div><button aria-label="展开会话" className="icon" onClick={()=>setExpanded(!expanded)}>{expanded?'收起':'展开'}</button></div><div className="mode">会话摘要<span>{session?session.id.slice(-8):'未连接'}</span></div><div ref={feed} className="feed" aria-live="polite">{!session?<div className="empty-session"><span>0{index+1}</span><h3>启动独立会话</h3><p>连接已安装的 Agent，查看公开回复与执行状态。</p><button disabled={busy||paused} onClick={start}>启动 {agent==='claude'?'Claude':agent==='codex'?'Codex':'OpenCode'} →</button></div>:messages.length===0?<p className="empty">{emptyMessage}</p>:messages.map(m=><div key={m.seq} className={'message '+m.type}><small>{eventSummary(m)}</small></div>)}</div><form onSubmit={async e=>{e.preventDefault();const input=text;if(await send(input))setText('')}}><textarea aria-label={`给 ${agent} 的指令`} placeholder={session?'输入指令，发送到此会话…':'启动会话后输入指令'} disabled={!session||session.state!=='ready'||paused} value={text} onChange={e=>setText(e.target.value)}/><div className="composer-actions"><div>{offline&&<button type="button" disabled={busy||paused} onClick={start}>恢复会话</button>}{session&&session.state==='ready'&&<><button type="button" onClick={takeover} disabled={busy}>接管</button><button type="button" onClick={interrupt} disabled={busy}>中断</button></>}</div><button className="primary" disabled={!session||!text.trim()||busy||paused||session.state!=='ready'}>发送 ↑</button></div></form></article>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
