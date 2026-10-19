import {useState} from 'react';
import type {Api} from './ProjectPanel';
type Policy={revision:number;source:string;fingerprint:string;config:unknown;active_tasks?:string[];uncovered_tasks?:string[];validation_error?:string};
export function AcceptancePanel({prefix,api}:{prefix:string;api:Api}) {
  const [policy,setPolicy]=useState<Policy|null>(null),[draft,setDraft]=useState(''),[message,setMessage]=useState(''),[busy,setBusy]=useState(false);
  async function load(){setBusy(true);setMessage('');try{const value=await api(prefix+'/acceptance');setPolicy(value);setDraft(JSON.stringify(value.config||{checks:{},protected_paths:[]},null,2))}catch(e){setMessage(String(e))}finally{setBusy(false)}}
  async function save(){if(!policy)return;setBusy(true);setMessage('');try{
    const config=JSON.parse(draft);const value=await api(prefix+'/acceptance',{fingerprint:policy.fingerprint,config});
    setPolicy(value);setDraft(JSON.stringify(value.config,null,2));setMessage('验收配置已保存。新执行使用新配置；受影响的进行中任务会在验收守卫处停止并等待对账。');
  }catch(e){setMessage(String(e)+'。编辑内容已保留；版本冲突时请复制内容，再重新载入比较。')}finally{setBusy(false)}}
  return <details className="panel"><summary>验收配置管理</summary>
    <p>配置独立验收命令及受保护路径。保存不会运行命令。首次保存后由项目数据库管理，原 acceptance.json 保留但不再作为生效配置。</p>
    <button disabled={busy} onClick={()=>void load()}>{policy?'重新载入（覆盖编辑内容）':'载入验收配置'}</button>
    {policy&&<><p>版本 {policy.revision} · 来源 {policy.source==='database'?'项目数据库':'本地文件'}</p>
      {policy.validation_error&&<p>当前配置需要修复：{policy.validation_error}</p>}
      {!!policy.active_tasks?.length&&<p>进行中任务：{policy.active_tasks.join('、')}。修改相关检查或保护路径会使原验收配置失效。</p>}
      {!!policy.uncovered_tasks?.length&&<p>缺少对应检查的任务：{policy.uncovered_tasks.join('、')}</p>}
      <label>验收配置 JSON<textarea disabled={busy} rows={14} value={draft} onChange={e=>setDraft(e.target.value)} spellCheck={false}/></label>
      <p>checks 将验收名称映射到参数数组，首项为可执行文件绝对路径，例如 Python 路径；其后为测试脚本和参数。protected_paths 使用项目相对路径及 / 分隔符。</p>
      <button disabled={busy} onClick={()=>void save()}>校验并保存验收配置</button></>}
    {message&&<p role="status">{message}</p>}
  </details>
}
