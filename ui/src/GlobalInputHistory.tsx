import {useEffect,useState} from 'react';
import type {Api} from './ProjectPanel';

type Entry={id:string;project_id:string;path:string;content:unknown;created_at:number};

export function GlobalInputHistory({api,usePlan}:{api:Api;usePlan:(goal:string,acceptance:string[])=>void}) {
  const [query,setQuery]=useState(''),[items,setItems]=useState<Entry[]>([]),[total,setTotal]=useState(0),[message,setMessage]=useState('');
  useEffect(()=>{let active=true;const timer=setTimeout(()=>{api('/api/v1/input-history?q='+encodeURIComponent(query)).then(data=>{if(active){setItems(data.items);setTotal(data.total)}}).catch(error=>{if(active)setMessage(String(error))})},200);return()=>{active=false;clearTimeout(timer)}},[api,query]);
  return <details className="panel"><summary>全局输入历史 · {total} 条</summary><p className="muted">跨项目保存经脱敏的操作输入。搜索历史目标或指令；“复用到 PM 计划”会回填项目目标和验收条件。</p><label>搜索<input value={query} onChange={event=>setQuery(event.target.value)} placeholder="关键词、项目目标或指令"/></label>{message&&<p role="alert">{message}</p>}<ul>{items.map(item=>{const plan=typeof item.content==='object'&&item.content!==null?item.content as {goal?:unknown;acceptance?:unknown}:{};const canUse=typeof plan.goal==='string';return <li key={item.id}><small>{new Date(item.created_at*1000).toLocaleString()} · {item.path}</small><pre>{JSON.stringify(item.content,null,2)}</pre>{canUse&&<button type="button" onClick={()=>usePlan(plan.goal as string,Array.isArray(plan.acceptance)?plan.acceptance.filter((value):value is string=>typeof value==='string'):[])}>复用到 PM 计划</button>}</li>})}</ul></details>
}
