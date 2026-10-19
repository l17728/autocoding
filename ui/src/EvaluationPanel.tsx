import {useState} from 'react';
import type {Api} from './ProjectPanel';
import {HoldoutPanel} from './HoldoutPanel';
export function EvaluationPanel({prefix,api}:{prefix:string;api:Api}){
  const [value,setValue]=useState<any>(null),[draft,setDraft]=useState(''),[busy,setBusy]=useState(false),[message,setMessage]=useState('');
  async function load(){const result=await api(prefix+'/learning-evaluations');setValue(result);setDraft(JSON.stringify(result.suite.cases,null,2))}
  async function action(work:()=>Promise<unknown>){setBusy(true);setMessage('');try{await work();await load()}catch(e){setMessage(String(e)+'；编辑内容保留。')}finally{setBusy(false)}}
  async function history(){setBusy(true);setMessage('');try{const result=await api(prefix+'/learning-evaluations/history-cases');
    if(result.cases.length)setDraft(JSON.stringify(result.cases,null,2));
    setMessage((result.cases.length?`已生成 ${result.cases.length} 条草稿。`:'暂无符合条件的历史答案，保留当前草稿。')+result.notice);
  }catch(e){setMessage(String(e))}finally{setBusy(false)}}
  return <details className="panel"><summary>知识效果对照评测</summary><p>同一场景分别在无知识、有知识的新会话中运行，随机先后顺序，按预先登记的 JSON 答案评分。必须包含适用范围外的反例；结果只反映此评测集，不代表生产效果。</p>
    <HoldoutPanel prefix={prefix} api={api}/>
    <button disabled={busy} onClick={()=>void action(load)}>载入评测</button>
    {value&&<><label>场景 JSON<textarea aria-label="场景 JSON" rows={10} value={draft} onChange={e=>setDraft(e.target.value)} disabled={busy}/></label>
      {value.suite.holdout_sources&&<p>当前使用经 Owner 复核的隔离样本，运行前后会检查复核状态和知识版本。手动编辑并保存 JSON 将成为普通评测集，不保留隔离样本来源保证。</p>}
      <p>每项包含 id、scope（inside / outside）、prompt（明确要求回答格式）、expected（预期 JSON 对象）。至少 2 项，最多 40 项。</p>
      <button disabled={busy} onClick={()=>void history()}>从用户历史答案生成草稿（替换当前草稿，不调用模型）</button>
      <button disabled={busy} onClick={()=>void action(()=>api(prefix+'/learning-evaluations/suite',{fingerprint:value.fingerprint,cases:JSON.parse(draft)}))}>保存评测集</button>
      <button disabled={busy||!value.suite.cases.length} onClick={()=>void action(()=>api(prefix+'/learning-evaluations/run',{}))}>运行成对评测（每场景两次模型调用）</button>
      {value.reports.map((r:any)=><article key={r.id}><h3>{r.state} · {r.current?'当前知识与场景':'历史版本'}</h3><p>{r.reason}</p>{r.summary&&<><p>样本 {r.summary.pairs} · 改善 {r.summary.wins} · 退化 {r.summary.losses} · 单侧配对检验 p={r.summary.paired_one_sided_p.toFixed(4)}</p><p>退化场景：{r.summary.regressions.join('、')||'无'} · 范围外反例 {r.summary.outside_scope_pairs}</p><p>{r.summary.candidate_for_owner_review?'达到评测集内的人工复核门槛；不会自动推广知识。':'证据不足或存在退化，不能认定改进有效。'}</p></>}</article>)}</>}
    {busy&&<p role="status">正在执行，长评测可能需要多分钟；请勿重复启动。</p>}{message&&<p role="status">{message}</p>}
  </details>
}
