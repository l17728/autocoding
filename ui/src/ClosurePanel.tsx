import {useEffect,useRef,useState} from 'react';
import type {Api} from './ProjectPanel';
type Report={id:string;created_at:number;summary:string;candidate_sha:string;integration_sha256:string;task_snapshot_current?:boolean;
  tasks:{id:string;description:string;state:string}[];
  bugfix_review:{run_id:string;task_id:string;state:string;failed_checks:string[];reason?:string;later_verified:boolean;artifact_sha256?:string}[];
  patterns:Record<string,number>;lessons:{content:string}[];limitations:string[];
  interaction_summary:{recorded_questions:number;help_requests:number;owner_answers:number}};
export function ClosurePanel({prefix,api}:{prefix:string;api:Api}) {
  const [reports,setReports]=useState<Report[]>([]),[message,setMessage]=useState(''),[busy,setBusy]=useState(false),[query,setQuery]=useState('');
  const revision=useRef(0);
  useEffect(()=>{let active=true;let timer:ReturnType<typeof setTimeout>;async function poll(){try{
    const version=revision.current;const result=await api(prefix+'/pm/closures');if(active&&version===revision.current)setReports(result);
  }catch(e){if(active)setMessage(String(e))}finally{if(active)timer=setTimeout(poll,15000)}}
  setReports([]);void poll();return()=>{active=false;clearTimeout(timer)};
  },[prefix]);
  async function generate(){revision.current++;setBusy(true);setMessage('');try{await api(prefix+'/pm/closures',{});revision.current++;setReports(await api(prefix+'/pm/closures'));setMessage('收尾报告已保存，经验已进入知识库。')}catch(e){setMessage(String(e))}finally{setBusy(false)}}
  return <details className="panel closure"><summary>项目收尾复盘与报告</summary>
    <p>全部登记任务完成且批次集成验收通过后自动生成。报告保存当时的交付、返工、求助和经验记录，可供后续复盘；后续新增工作不改变旧报告。</p>
    <label>查找收尾报告<input value={query} onChange={e=>setQuery(e.target.value)} placeholder="任务、失败检查或经验关键词"/></label>
    <button disabled={busy} onClick={()=>void generate()}>{busy?'生成中…':'生成或重试收尾报告'}</button>
    {!reports.length&&<p>尚无收尾报告。完成任务和集成验收后将在这里展示。</p>}
    {!!reports.length&&!reports.some(r=>JSON.stringify(r).toLowerCase().includes(query.toLowerCase()))&&<p role="status">没有匹配的报告，请更换关键词。</p>}
    {reports.filter(r=>JSON.stringify(r).toLowerCase().includes(query.toLowerCase())).map(r=><article key={r.id}>
      <h3>{new Date(r.created_at*1000).toLocaleString()} · 项目复盘</h3><p>{r.summary}</p>
      <p className="muted">{r.task_snapshot_current===false?'历史快照，项目已变化':r.task_snapshot_current===true?'任务快照与当前项目一致':'历史报告，当前任务状态尚未核对'}</p>
      <p>已记录问答 {r.interaction_summary.recorded_questions} 次，其中求助 {r.interaction_summary.help_requests} 次、用户回答 {r.interaction_summary.owner_answers} 次。</p>
      <details><summary>交付任务与集成证据</summary><ul>{r.tasks.map(t=><li key={t.id}>{t.description} · {t.state}</li>)}</ul><p>候选提交：<code>{r.candidate_sha}</code></p><p>验收报告摘要：<code>{r.integration_sha256}</code></p></details>
      <details><summary>返工与 bugfix 总结 · {r.bugfix_review.length} 条</summary>{r.bugfix_review.map(f=><p key={f.run_id}>{f.task_id}：{f.failed_checks.join('、')||f.reason||f.state}；{f.later_verified?'后来有同任务验收通过记录':'尚无后续通过记录'}</p>)}<ul>{Object.entries(r.patterns).map(([pattern,count])=><li key={pattern}>{pattern} · {count} 次</li>)}</ul></details>
      <h4>经验教训</h4><ul>{r.lessons.map((l,i)=><li key={i}>{l.content}</li>)}</ul>
      <details><summary>结论适用范围</summary><ul>{r.limitations.map((l,i)=><li key={i}>{l}</li>)}</ul></details>
    </article>)}{message&&<p role="status">{message}</p>}
  </details>;
}
