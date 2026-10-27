import {useEffect,useRef,useState} from 'react';
import type {Api} from './ProjectPanel';
import {OwnerProfilePanel} from './OwnerProfilePanel';
type Item={category:string;content:string;confidence:string;evidence_ids:string[];sources?:{kind:string;id?:string;url?:string}[]};
type Review={id:string;state:string;summary?:string;error?:string;items:Item[];archived:boolean;created_at:number;from_seq:number;to_seq:number};
type Knowledge={id:string;topic:string;revision:string;variants:(Item&{id:string;state:string;sources:{review_id:string}[]})[]};
type Interaction={id:string;kind:string;content:unknown;state:string;request_reason?:string;reason?:string;answers?:unknown;answered_by?:string;context?:string;same_question_requests:number;created_at:number;delivery_state?:string};
type InteractionPage={total:number;requests:number;owner_answers:number;repeat_requests:number;pending:number;items:Interaction[];notice:string};
type State={settings:{enabled:boolean;interval_minutes:number};reviews:Review[];principles:string[];knowledge:Knowledge[];knowledge_total:number;self_check?:{task_states:Record<string,number>;pending_owner:number;repeat_requests:number;notice:string};outcomes:{current:{samples:number;score:number|null};previous:{samples:number;score:number|null};delta:number|null;interpretation:string};health:{state?:string;code?:string;failures?:number;at?:number;next_retry_at?:number}};
const names:Record<string,string>={communication:'话术总结',lesson:'经验教训',principle:'工作原则',improvement:'改进动作'};
export function ReflectionPanel({prefix,api,onHealth}:{prefix:string;api:Api;onHealth?:(health:State['health'])=>void}) {
  const [data,setData]=useState<State|null>(null),[busy,setBusy]=useState(false),[message,setMessage]=useState('');
  const [enabled,setEnabled]=useState(true),[minutes,setMinutes]=useState(60);
  const [offset,setOffset]=useState(0);
  const revision=useRef(0);
  const [interactionOffset,setInteractionOffset]=useState(0),[interactions,setInteractions]=useState<InteractionPage|null>(null);
  useEffect(()=>{let active=true;let timer:ReturnType<typeof setTimeout>;async function poll(){try{
    const next=await api(prefix+'/pm/interactions?offset='+interactionOffset);if(active)setInteractions(next);
  }catch(e){if(active)setMessage(String(e))}finally{if(active)timer=setTimeout(poll,15000)}}
  setInteractions(null);void poll();return()=>{active=false;clearTimeout(timer)};
  },[prefix,interactionOffset]);
  const reviewUrl=prefix+'/pm/reviews?offset='+offset;
  useEffect(()=>{let active=true;let timer:ReturnType<typeof setTimeout>;async function poll(){try{
    const version=revision.current;const next=await api(reviewUrl);if(active&&version===revision.current)setData(next);
  }catch(e){if(active)setMessage(String(e))}finally{if(active)timer=setTimeout(poll,15000)}}
  void poll();return()=>{active=false;clearTimeout(timer)};
  },[prefix,offset]);
  useEffect(()=>{if(data?.health)onHealth?.(data.health)},[data?.health,onHealth]);
  useEffect(()=>{let active=true;api(prefix+'/pm/reviews').then(next=>{if(active){setEnabled(next.settings.enabled);setMinutes(next.settings.interval_minutes)}}).catch(e=>{if(active)setMessage(String(e))});return()=>{active=false}},[prefix]);
  async function action(work:()=>Promise<any>){revision.current++;setBusy(true);setMessage('');try{const result=await work();revision.current++;setData(await api(reviewUrl));if(result?.state)setMessage(result.state==='no_new_evidence'?'没有新的活动可复盘。':result.state==='completed'?'自检完成，经验已进入本项目知识库。':`自检状态：${result.state}`)}catch(e){setMessage(String(e))}finally{setBusy(false)}}
  return <details className="panel reflection"><summary>PM 自检与知识库</summary>
    <OwnerProfilePanel key={prefix} prefix={prefix} api={api}/>
    {data?.self_check&&<section aria-label="PM 推进自查"><h3>推进自查</h3><p>待办 {data.self_check.task_states.todo||0} · 阻塞 {data.self_check.task_states.blocked||0} · 待用户回答 {data.self_check.pending_owner} · 重复求助 {data.self_check.repeat_requests}</p><p className="muted">{data.self_check.notice}</p></section>}
    <p>复盘 PM 自己的计划、沟通、判断与验收结果，将有来源的经验用于下一次工作。定期轮换维护冷启动知识和后续经验，检查准确性、适用条件、缺口与冗余，让流程、话术和原则更简洁。自学习通过经验检索实现，不修改模型权重。</p>
    <form onSubmit={e=>{e.preventDefault();void action(()=>api(prefix+'/pm/reviews/settings',{enabled,interval_minutes:minutes}))}}>
      <label className="check-label"><input type="checkbox" checked={enabled} onChange={e=>setEnabled(e.target.checked)}/>启用定期自检</label>
      <label>自检间隔（分钟）<input type="number" min={5} max={10080} value={minutes} onChange={e=>setMinutes(Number(e.target.value))}/></label>
      <button disabled={busy}>保存自检设置</button><button type="button" disabled={busy} onClick={()=>void action(()=>api(prefix+'/pm/reviews',{}))}>{busy?'正在处理…':'立即自检'}</button>
      <p className="muted">工作台运行且有新增活动时才调用模型；暂停项目会停止自动自检。自检使用已配置的模型额度。</p>
    </form><h3>固定核心原则</h3><ul>{data?.principles.map(p=><li key={p}>{p}</li>)}</ul>
    <h3>执行结果趋势</h3>{data?.outcomes&&<div><p>最近执行：{data.outcomes.current.samples} 次，独立验收通过率 {data.outcomes.current.score===null?'暂无数据':`${data.outcomes.current.score}%`}。前一窗口：{data.outcomes.previous.samples} 次。</p><p>{data.outcomes.delta===null?'样本不足，暂不判断改善。':`与前一窗口相比：${data.outcomes.delta>0?'+':''}${data.outcomes.delta} 个百分点。`}</p><p className="muted">{data.outcomes.interpretation}</p></div>}
    {data?.health.state==='error'&&<p role="alert">定时巡检异常：{data.health.code}，下个周期将重新检查。</p>}
    <h3>求助与交互记录</h3>
    {interactions&&<><p>交互 {interactions.total} 次 · 向用户求助 {interactions.requests} 次 · 用户回答 {interactions.owner_answers} 次 · 重复求助 {interactions.repeat_requests} 次 · 待回答 {interactions.pending} 次</p>
      <p className="muted">{interactions.notice} 历史回答用于判断同类问题，不能替代当前授权；模型归纳出的用户偏好仍需核验。</p>
      <button disabled={interactionOffset===0} onClick={()=>setInteractionOffset(Math.max(0,interactionOffset-25))}>上一页交互</button>
      <button disabled={interactionOffset+25>=interactions.total} onClick={()=>setInteractionOffset(interactionOffset+25)}>下一页交互</button>
      {interactions.items.map(item=><details key={item.id}><summary>{item.kind==='permission'?'权限确认':'开发问答'} · {item.state} · 同题求助 {item.same_question_requests} 次</summary>
        <p>{new Date(item.created_at*1000).toLocaleString()}</p><pre>{JSON.stringify(item.content,null,2)}</pre>
        {item.context&&<p>任务背景：{item.context}</p>}<p>求助原因：{item.request_reason||'未向用户求助'}</p>
        {item.reason&&<p>决策依据：{item.reason}</p>}<p>回答者：{item.answered_by||'暂无'}；回答：{JSON.stringify(item.answers??'暂无')}</p>
        {item.delivery_state&&<p>投递状态：{item.delivery_state}</p>}
      </details>)}
    </>}
    <h3>知识治理</h3><p className="muted">相同内容合并来源；同主题及测试、权限、未知投递、凭据领域的中英文候选分歧暂停引用，选择后淘汰其他版本；超过 90 天的经验停止自动引用。领域检测可能将互补建议列为候选，需核对，不能识别所有语义冲突。</p>
    <p className="muted">冷启动知识的修订进入原主题，保留来源与旧版本；有分歧时由你选择采用版本，不因模型改写就自动认为更好。</p>
    <p>共 {data?.knowledge_total||0} 个主题，每页 25 个。<button disabled={busy||offset===0} onClick={()=>setOffset(Math.max(0,offset-25))}>上一页</button><button disabled={busy||offset+25>=(data?.knowledge_total||0)} onClick={()=>setOffset(offset+25)}>下一页</button></p>
    {data?.knowledge?.map(group=><article key={group.id}><strong>{group.topic}</strong>{group.variants.map(v=><div key={v.id}><p>{v.content}</p><small>{({active:'参与引用',conflict:'待处理分歧',superseded:'已淘汰',expired:'已过期'} as Record<string,string>)[v.state]} · {v.sources.length} 条来源</small>{v.state==='conflict'&&<button disabled={busy} onClick={()=>void action(()=>api(prefix+`/pm/knowledge/${group.id}/resolve`,{selected:v.id,revision:group.revision}))}>采用此建议</button>}</div>)}</article>)}
    <h3>自检记录与经验</h3>{data?.reviews.length===0&&<p className="muted">完成计划、问答或开发任务后，可生成第一份自检。</p>}
    {data?.reviews.map(review=><article className="review-record" key={review.id}><strong>{review.summary||review.error||review.state}</strong>
      <p className="muted">{new Date(review.created_at*1000).toLocaleString()} · 事件 #{review.from_seq}–#{review.to_seq} · {review.archived?'已归档，不参与后续检索':review.state==='completed'?'经验参考':'尚未形成知识'}</p>
      {review.items.map((item,index)=><div key={index}><b>{names[item.category]} · {item.confidence==='observed'?'观察结论':'待验证假设'}</b><p>{item.content}</p><details><summary>证据引用</summary><code>{item.evidence_ids.join(', ')}</code>{item.sources?.map((source,i)=><p key={i}>{source.kind==='repository'&&source.url?<a href={source.url} target="_blank" rel="noreferrer">规则仓库固定版本来源</a>:<>本地用户指令摘要 ID：<code>{source.id}</code></>}</p>)}</details></div>)}
      {review.state==='completed'&&<button disabled={busy} onClick={()=>void action(()=>api(prefix+`/pm/reviews/${review.id}/archive`,{archived:!review.archived}))}>{review.archived?'恢复为参考':'归档此经验'}</button>}
    </article>)}{message&&<p role="status">{message}</p>}
  </details>
}
