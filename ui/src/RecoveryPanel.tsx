import {useRef,useState} from 'react';
import type {Api} from './ProjectPanel';

type Run={id:string;task_id:string;state:string;reason?:string;workspace_id?:string};
type Evidence={fingerprint:string;unavailable_reason:string|null;run:Run;missing_workspace?:boolean;orphan?:boolean;resolution?:{note:string;at:number;fingerprint:string}|null;
  task:{revision:number;attempt:number};session:{id:string;epoch:number;generation:number}|null;
  workspace:{path:string;head:string;status:string}|null;source_head:string;
  commands:{id:string;state:string}[];questions:{id:string;state:string}[];approvals:{id:string;state:string;delivery_state:string|null}[]};
export function RecoveryPanel({prefix,api,runs,tasks=[]}:{prefix:string;api:Api;runs:Run[];tasks?:{id:string;state:string;reason?:string}[]}) {
  const [evidence,setEvidence]=useState<Evidence|null>(null),[note,setNote]=useState(''),[stopped,setStopped]=useState(false);
  const [acceptMissing,setAcceptMissing]=useState(false);
  const [repair,setRepair]=useState<{run:string;fingerprint:string;record:{path:string};head_sha:string}|null>(null);
  const candidates=[...runs,...tasks.filter(t=>t.state==='blocked'&&!runs.some(r=>r.task_id===t.id)).map(t=>({id:'orphan:'+t.id,task_id:t.id,state:'reconciling',reason:'运行记录缺失：'+(t.reason||'')}))];
  const [message,setMessage]=useState(''),[busy,setBusy]=useState(false);
  const request=useRef(0);
  async function registration(id:string){setEvidence(null);setBusy(true);setStopped(false);setRepair(null);setMessage('');try{
    setRepair({...await api(prefix+'/runs/'+id+'/registration'),run:id});
  }catch(e){setMessage(String(e))}finally{setBusy(false)}}
  async function fixRegistration(){if(!repair)return;setBusy(true);try{
    await api(prefix+'/runs/'+repair.run+'/registration',{fingerprint:repair.fingerprint,stopped});
    setRepair(null);setMessage('注册信息已修复，旧注册文件已备份；工作树内容保持不变。可重新核对恢复证据。');
  }catch(e){setMessage(String(e))}finally{setBusy(false)}}
  async function inspect(id:string){const version=++request.current;setRepair(null);setBusy(true);setEvidence(null);setStopped(false);setAcceptMissing(false);setNote('');setMessage('');
    try{const result=await api(prefix+'/runs/'+id+'/recovery');if(version===request.current)setEvidence(result)}
    catch(e){if(version===request.current)setMessage(String(e))}finally{if(version===request.current)setBusy(false)}}
  async function resolve(){if(!evidence)return;setBusy(true);setMessage('');try{
    await api(prefix+'/runs/'+evidence.run.id+'/recovery',{fingerprint:evidence.fingerprint,note,stopped,...(evidence.missing_workspace?{accept_missing_workspace:acceptMissing}:{})});
    setEvidence(null);setStopped(false);setNote('');setMessage('旧尝试已归档，任务已允许重新执行。请从任务入口启动新尝试；未重发旧命令，未覆盖已有工作树。');
  }catch(e){setStopped(false);setMessage(String(e)+'；请重新核对证据后操作。')}finally{setBusy(false)}}
  return <details className="panel"><summary>异常对账与恢复</summary>
    <p>核查旧执行已停止及外部副作用后，可结束旧尝试并允许重新开发。新尝试使用独立工作树，旧修改保留但不会自动合并。</p>
    {runs.filter(r=>r.workspace_id&&['reconciling','failed'].includes(r.state)).map(r=><p key={'repair:'+r.id}>{r.task_id} <button disabled={busy} onClick={()=>void registration(r.id)}>诊断工作树注册</button></p>)}
    {repair&&<div><p>已与 Git 注册及仓库关联核对：{repair.record.path}<br/>当前提交：{repair.head_sha}</p>
      <label><input type="checkbox" checked={stopped} onChange={e=>setStopped(e.target.checked)}/>确认该工作树的执行已停止</label>
      <button disabled={busy||!stopped} onClick={()=>void fixRegistration()}>备份并修复注册信息</button></div>}
    {candidates.filter(r=>r.state==='reconciling').map(r=><p key={r.id}><b>{r.task_id}</b> · {r.reason} <button disabled={busy} onClick={()=>void inspect(r.id)}>核对恢复证据</button></p>)}
    {!candidates.some(r=>r.state==='reconciling')&&<p>暂无等待对账的托管尝试。</p>}
    {runs.filter(r=>r.state==='reconciled').map(r=><p key={r.id}>{r.task_id} · 已对账 <button disabled={busy} onClick={()=>void inspect(r.id)}>查看对账记录</button></p>)}
    {evidence&&<div>
      {evidence.resolution&&<p>对账记录：{new Date(evidence.resolution.at*1000).toLocaleString()} · {evidence.resolution.note}<br/>当时证据指纹：{evidence.resolution.fingerprint}</p>}
      <p>任务 {evidence.run.task_id} · 版本 {evidence.task.revision} · 尝试 {evidence.task.attempt}</p>
      <p>会话 {evidence.session?.id||'未建立'} · 代次 {evidence.session?.generation} · 控制版本 {evidence.session?.epoch}</p>
      <p>旧工作树：{evidence.workspace?.path||'未建立'}<br/>旧提交：{evidence.workspace?.head||'无'}<br/>项目当前提交：{evidence.source_head}</p>
      <pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{evidence.workspace?.status||'无未提交变更'}</pre>
      <p>命令（原始结果保留）：{evidence.commands.map(c=>c.id+': '+c.state).join('；')||'无'}</p>
      <p>问题：{evidence.questions.map(q=>q.id+': '+q.state).join('；')||'无'}</p>
      <p>审批：{evidence.approvals.map(a=>a.id+': '+a.state+' / '+(a.delivery_state||'未投递')).join('；')||'无'}</p>
      {evidence.unavailable_reason&&<p role="alert">暂不能恢复：{evidence.unavailable_reason}</p>}
      {evidence.orphan&&<p role="alert">此任务缺少运行记录，不能自动关联旧会话和工作树。请核查项目全部相关执行；本次操作只释放任务，不清理或重放其他会话。</p>}
      {evidence.missing_workspace&&<label><input type="checkbox" checked={acceptMissing} onChange={e=>setAcceptMissing(e.target.checked)}/>旧工作树不可用，我接受无法自动恢复其中修改，从项目基线重新开发</label>}
      <label>核查结论<textarea value={note} onChange={e=>setNote(e.target.value)} maxLength={2000} placeholder="记录停止情况、未知命令副作用核查结果和重新执行的依据"/></label>
      <label><input type="checkbox" checked={stopped} onChange={e=>setStopped(e.target.checked)}/>我已核查旧执行及其子进程/远端操作已停止，未知副作用已处理，允许新尝试</label>
      <button disabled={busy||!stopped||note.trim().length<3||!!evidence.unavailable_reason||!!evidence.missing_workspace&&!acceptMissing} onClick={()=>void resolve()}>归档旧尝试并允许重新执行</button>
    </div>}
    {message&&<p role="status">{message}</p>}
  </details>
}
