import {useState} from 'react';
import type {Api} from './ProjectPanel';
type Finding={id:string;title:string;severity:string;location:string;recommendation:string;state:string;task_id?:string;fix_task_state?:string};
type Report={id:string;candidate_sha:string;current:boolean;state:string;reason?:string;findings:Finding[];reviews:{discipline:string;limitations:string}[]};
export function ExpertPanel({prefix,api,batches}:{prefix:string;api:Api;batches:{id:string;state:string}[]}) {
  const [data,setData]=useState<{settings:{enabled:boolean};reports:Report[]}|null>(null),[busy,setBusy]=useState(false),[message,setMessage]=useState('');
  async function load(){setData(await api(prefix+'/expert-reviews'))}
  async function action(work:()=>Promise<unknown>){setBusy(true);setMessage('');try{await work();await load()}catch(e){setMessage(String(e))}finally{setBusy(false)}}
  return <details className="panel"><summary>独立专家审查</summary><p>三个独立新会话分别检查正确性与测试、可靠性与性能、易用性与完整性。审查针对集成差异，不代表全仓库或真实运行验收；调用会使用 PM 模型额度。</p>
    <button disabled={busy} onClick={()=>void action(load)}>载入审查记录</button>
    {data&&<><label><input type="checkbox" checked={data.settings.enabled} disabled={busy} onChange={e=>void action(()=>api(prefix+'/expert-reviews/settings',{enabled:e.target.checked}))}/>集成后自动审查，并以当前候选审查结果作为收尾门禁</label>
    {batches.filter(b=>b.state==='verified_candidate').slice(-3).map(b=><button key={b.id} disabled={busy} onClick={()=>void action(()=>api(prefix+'/expert-reviews',{batch_id:b.id}))}>审查候选 {b.id.slice(-8)}</button>)}
    {data.reports.map(r=><article key={r.id}><h3>{r.candidate_sha.slice(0,12)} · {r.state} · {r.current?'当前证据':'证据已过期'}</h3>{r.reason&&<p>{r.reason}</p>}
      {r.reviews.map(s=><p key={s.discipline}>{s.discipline}：{s.limitations}</p>)}
      {r.findings.map(f=><FindingItem key={f.id} finding={f} busy={busy} save={(state,note,task_id)=>action(()=>api(prefix+'/expert-reviews/'+r.id+'/findings/'+f.id,{state,note,task_id}))}/>)}
      {!r.findings.length&&<p>当前记录无发现；仅 completed 状态表示三场审查均已完成。</p>}</article>)}</>}
    {busy&&<p role="status">正在处理；模型审查可能需要数分钟。</p>}{message&&<p role="status">{message}</p>}
  </details>
}
function FindingItem({finding:f,busy,save}:{finding:Finding;busy:boolean;save:(state:string,note:string,task:string|null)=>Promise<unknown>}){
  const [note,setNote]=useState(''),[task,setTask]=useState(f.task_id||'');
  return <div><b>{f.severity} · {f.title}</b><p>{f.location} · {f.recommendation}</p><p>{f.state} {f.fix_task_state&&'· 修复任务 '+f.fix_task_state+'，仍需新候选复审'}</p>
    <label>处理依据<input aria-label="处理依据" value={note} onChange={e=>setNote(e.target.value)}/></label><label>修复任务 ID<input aria-label="修复任务 ID" value={task} onChange={e=>setTask(e.target.value)}/></label>
    <button disabled={busy||note.trim().length<3||!task} onClick={()=>void save('accepted',note,task)}>采用并关联任务</button>
    <button disabled={busy||note.trim().length<3} onClick={()=>void save('dismissed',note,null)}>记录不采用理由</button>
    <button disabled={busy||note.trim().length<3} onClick={()=>void save('open',note,null)}>重新打开</button>
  </div>
}
