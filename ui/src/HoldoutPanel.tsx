import {useState} from 'react';
import type {Api} from './ProjectPanel';

export function HoldoutPanel({prefix,api}:{prefix:string;api:Api}){
  const [data,setData]=useState<any>(null),[source,setSource]=useState(''),[prompt,setPrompt]=useState('');
  const [scope,setScope]=useState('inside'),[answers,setAnswers]=useState<Record<string,string>>({});
  const [busy,setBusy]=useState(false),[message,setMessage]=useState('');
  const [notes,setNotes]=useState<Record<string,string>>({}),[selected,setSelected]=useState<string[]>([]),[manifest,setManifest]=useState<any>(null);
  const states:Record<string,string>={awaiting_answer:'待填写标准答案',pending_independence_review:'待独立性复核',accepted:'Owner 复核通过',rejected:'已驳回',contaminated:'已污染（不可恢复）'};
  const path=prefix+'/learning-evaluations/holdouts';
  async function load(){setData(await api(path))}
  async function action(work:()=>Promise<unknown>){setBusy(true);setMessage('');try{await work();await load()}catch(e){
    const error=String(e),messages:Record<string,string>={STALE_HOLDOUT_REVIEW:'样本已被更新，请重新查看样本后复核。',HOLDOUT_REVIEW_INVALIDATED:'样本复核已失效，请核查污染状态或知识变化后重新复核。',EVALUATION_COUNTEREXAMPLES_REQUIRED:'请同时选择范围内场景和范围外反例。',DUPLICATE_HOLDOUT_SCENE:'已有规范化相同的场景，请查看现有记录。'};
    setMessage(Object.entries(messages).find(([code])=>error.includes(code))?.[1]||error);
  }finally{setBusy(false)}}
  return <details><summary>隔离采集新的评测场景</summary>
    <p>这里的场景和答案不会自动进入 PM 学习、历史答案或复盘。请登记尚未用于训练的新场景及来源，不填写凭据。登记后冻结当时知识与偏好，再填写标准答案；完成采集仍须人工核查独立性。</p>
    <button disabled={busy} onClick={()=>void action(load)}>查看隔离样本</button>
    <label>样本来源与背景<input value={source} onChange={e=>setSource(e.target.value)} maxLength={1000} disabled={busy}/></label>
    <label>新评测场景<textarea value={prompt} onChange={e=>setPrompt(e.target.value)} maxLength={4000} disabled={busy}/></label>
    <label>场景范围<select aria-label="场景范围" value={scope} onChange={e=>setScope(e.target.value)} disabled={busy}><option value="inside">适用范围内</option><option value="outside">范围外反例</option></select></label>
    <button disabled={busy||!source.trim()||!prompt.trim()} onClick={()=>void action(async()=>{await api(path,{source,prompt,scope});setSource('');setPrompt('')})}>登记场景并冻结基线</button>
    {data&&<><p>{data.notice} 共 {data.total} 条，展示最近 {data.items.length} 条；人工复核不等于已独立证明。</p>
      {data.items.map((r:any)=><article key={r.id}><h4>{r.source}</h4><p>{r.prompt}</p><p>{states[r.state]} · {r.scope==='inside'?'范围内':'范围外'} · 版本 {r.revision}{r.state==='accepted'&&!r.review_current?' · 知识或偏好已变化，需重新复核':''}</p>
        {r.state==='awaiting_answer'?<><label>标准答案 JSON<textarea value={answers[r.id]||''} onChange={e=>setAnswers({...answers,[r.id]:e.target.value})} disabled={busy}/></label>
          <button disabled={busy||!answers[r.id]} onClick={()=>void action(()=>api(path+'/'+r.id+'/answer',{expected:JSON.parse(answers[r.id])}))}>保存标准答案（不可覆盖）</button></>:<pre>{JSON.stringify(r.expected,null,2)}</pre>}
        {r.state!=='awaiting_answer'&&r.state!=='contaminated'&&<><label>复核依据<textarea value={notes[r.id]||''} onChange={e=>setNotes({...notes,[r.id]:e.target.value})} disabled={busy} placeholder="说明来源、语义重叠核查、范围与答案依据，至少 10 字"/></label>
          {(['accepted','rejected','contaminated'] as const).map(decision=><button key={decision} disabled={busy||(notes[r.id]||'').trim().length<10}
            onClick={()=>void action(()=>api(path+'/'+r.id+'/review',{revision:r.revision,decision,note:notes[r.id]}))}>{decision==='accepted'?'复核通过':decision==='rejected'?'驳回样本':'标记污染（不可恢复）'}</button>)}</>}
        {r.reviews?.map((v:any)=><p key={v.revision}>复核版本 {v.revision} · {states[v.decision]}：{v.note}</p>)}
        {r.state==='accepted'&&r.review_current&&<label><input type="checkbox" checked={selected.includes(r.id)} disabled={busy} onChange={e=>setSelected(e.target.checked?[...selected,r.id]:selected.filter(id=>id!==r.id))}/>选择导出样本</label>}
      </article>)}</>}
    <button disabled={busy||selected.length<2} onClick={()=>void action(async()=>setManifest(await api(prefix+'/learning-evaluations/holdout-exports',{sample_ids:selected})))}>生成版本化导出（须含范围外反例）</button>
    {data?.exports?.map((r:any)=><button key={r.id} disabled={busy} onClick={()=>void action(async()=>setManifest(await api(prefix+'/learning-evaluations/holdout-exports/'+r.id)))}>查看历史导出 {r.id}</button>)}
    {manifest&&<><p>导出版本：{manifest.id}；摘要：{manifest.content_hash}</p>
      {manifest.current===false&&<p>此导出的复核已失效，可下载历史记录；需重新复核并导出后才能接入评测。</p>}
      <button disabled={busy} onClick={()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(manifest,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download=manifest.id+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}}>下载评测清单 JSON</button>
      <button disabled={busy||manifest.current===false} onClick={()=>void action(async()=>{const current=await api(prefix+'/learning-evaluations');await api(prefix+'/learning-evaluations/holdout-suite',{export_id:manifest.id,fingerprint:current.fingerprint});setMessage('已接入评测集。请载入评测后运行；尚未调用模型。')})}>接入成对评测集（替换当前集，不调用模型）</button></>}
    {message&&<p role="status">{message}</p>}
  </details>
}
