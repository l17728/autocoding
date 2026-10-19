import {useState} from 'react';
import type {Api} from './ProjectPanel';
export function HistoryPanel({prefix,api}:{prefix:string;api:Api}){
  const [kind,setKind]=useState('runs'),[page,setPage]=useState<any>(null),[busy,setBusy]=useState(false),[message,setMessage]=useState('');
  async function load(offset=0){setBusy(true);try{setPage(await api(prefix+'/history/'+kind+'?offset='+offset+'&limit=20'));setMessage('')}catch(e){setMessage(String(e))}finally{setBusy(false)}}
  return <details className="panel"><summary>分页历史记录</summary><p>工作台只载入最近或待处理记录。这里按页查询完整历史；超出单页内容预算的记录会明确标记 payload_omitted。</p>
    <select disabled={busy} value={kind} onChange={e=>{setKind(e.target.value);setPage(null)}}>{Object.entries({runs:'运行',batches:'批次',sessions:'会话',commands:'命令',approvals:'审批',questions:'问答',events:'审计事件',closures:'收尾报告',expert_reviews:'专家审查',evaluations:'知识评测'}).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select>
    <button disabled={busy} onClick={()=>void load()}>查询历史</button>
    {page&&<><p>共 {page.total} 条 · 从第 {page.offset+1} 条开始</p><button disabled={busy||page.offset===0} onClick={()=>void load(Math.max(0,page.offset-page.limit))}>上一页</button><button disabled={busy||page.next_offset===null} onClick={()=>void load(page.next_offset)}>下一页</button><pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(page.items,null,2)}</pre></>}
    {message&&<p role="status">{message}</p>}
  </details>
}
