import {useEffect,useState} from 'react';
import type {Api} from './ProjectPanel';

type Policy={revision:number;source:string;config:{checks?:Record<string,string[]>};active_tasks?:string[];uncovered_tasks?:string[];validation_error?:string};

export function AcceptancePanel({prefix,api}:{prefix:string;api:Api}) {
  const [policy,setPolicy]=useState<Policy|null>(null),[message,setMessage]=useState(''),[busy,setBusy]=useState(false);
  async function bootstrap(){setBusy(true);setMessage('');try{await api(prefix+'/acceptance/bootstrap',{});const next=await api(prefix+'/acceptance');setPolicy(next);setMessage('PM 已生成并启用项目自动验收基线。业务验收标准会保留在计划上下文中，执行命令由 PM 维护。')}catch(e){setMessage(String(e))}finally{setBusy(false)}}
  useEffect(()=>{void bootstrap()},[prefix]);
  return <details className="panel"><summary>PM 自动验收</summary>
    <p>无需维护 JSON、命令或本地路径。PM 会根据项目结构维护独立验证基线，并在任务执行前自动补齐。</p>
    <button disabled={busy} onClick={()=>void bootstrap()}>{busy?'正在准备自动验收…':'重新生成自动验收基线'}</button>
    {policy&&<><p>状态：{policy.validation_error?'正在修复自动验收':'已就绪'} · 自动检查：{Object.keys(policy.config?.checks||{}).join('、')||'准备中'}</p>
      {!!policy.active_tasks?.length&&<p>进行中任务：{policy.active_tasks.join('、')}。</p>}
      {!!policy.uncovered_tasks?.length&&<p>待迁移任务：{policy.uncovered_tasks.join('、')}。</p>}</>}
    {message&&<p role="status">{message}</p>}
  </details>
}
