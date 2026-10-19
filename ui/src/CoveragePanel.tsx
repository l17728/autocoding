import {useState} from 'react';
import type {Api} from './ProjectPanel';
type Case={task_id:string;criterion:string;status:string;artifact_sha256:string|null};
type Matrix={fingerprint:string;revision:number;requirements:unknown[];matrix:{id:string;description:string;status:string;cases:Case[]}[];unmapped_tasks:string[];unmapped_checks?:{task_id:string;criterion:string}[]};
export function CoveragePanel({prefix,api}:{prefix:string;api:Api}) {
  const [value,setValue]=useState<Matrix|null>(null),[draft,setDraft]=useState(''),[busy,setBusy]=useState(false),[message,setMessage]=useState('');
  async function load(){setBusy(true);try{const result=await api(prefix+'/coverage');setValue(result);setDraft(JSON.stringify(result.requirements,null,2));setMessage('')}catch(e){setMessage(String(e))}finally{setBusy(false)}}
  async function save(){if(!value)return;setBusy(true);try{await api(prefix+'/coverage',{fingerprint:value.fingerprint,requirements:JSON.parse(draft)});await load()}catch(e){setMessage(String(e)+'；草稿已保留。')}finally{setBusy(false)}}
  return <details className="panel"><summary>需求与验收覆盖矩阵</summary>
    <p>登记需求并关联任务的验收检查。登记后，缺少映射或当前证据会阻止收尾；只有任务版本、检查配置及审计证据一致才显示通过。一个测试命令通过不代表其中每个用例都已登记。</p>
    <button disabled={busy} onClick={()=>void load()}>载入覆盖矩阵</button>
    {value&&<><p>版本 {value.revision} · 未关联需求的任务：{value.unmapped_tasks.join('、')||'无'}</p>
      {!!value.unmapped_checks?.length&&<p>尚未关联的检查：{value.unmapped_checks.map(c=>c.task_id+'/'+c.criterion).join('、')}</p>}
      {value.matrix.map(r=><article key={r.id}><strong>{r.description} · {r.status==='passed'?'已登记检查通过':'证据不完备'}</strong>{r.cases.map(c=><p key={c.task_id+':'+c.criterion}>{c.task_id} / {c.criterion} · {c.status==='passed'?'通过':'未证明'}<small>{c.artifact_sha256&&' SHA-256: '+c.artifact_sha256}</small></p>)}</article>)}
      <label>需求 JSON<textarea aria-label="需求 JSON" rows={10} value={draft} disabled={busy} onChange={e=>setDraft(e.target.value)}/></label>
      <p>格式：{JSON.stringify([{id:'REQ-1',description:'需求说明',cases:[{task_id:'task_example',criterion:'unit'}]}])}。cases 为空表示尚无覆盖。</p>
      <button disabled={busy} onClick={()=>void save()}>保存需求映射</button></>}
    {message&&<p role="status">{message}</p>}
  </details>;
}
