import {useEffect,useState} from 'react';
import type {Api} from './ProjectPanel';
type Preferences={working_style:string;decision_style:string;reporting_style:string};
type Profile={revision:number;source:string;preferences:Preferences};
const labels:Record<keyof Preferences,string>={working_style:'推进方式',decision_style:'判断与求助方式',reporting_style:'沟通风格'};
export function OwnerProfilePanel({prefix,api}:{prefix:string;api:Api}){
  const [profile,setProfile]=useState<Profile|null>(null),[draft,setDraft]=useState<Preferences|null>(null);
  const [busy,setBusy]=useState(false),[message,setMessage]=useState('');
  useEffect(()=>{let active=true;api(prefix+'/pm/profile').then(value=>{if(active){setProfile(value);setDraft(value.preferences)}})
    .catch(e=>{if(active)setMessage(String(e))});return()=>{active=false}},[prefix]);
  async function save(){if(!profile||!draft)return;setBusy(true);setMessage('');try{
    const value=await api(prefix+'/pm/profile',{revision:profile.revision,preferences:draft});setProfile(value);setDraft(value.preferences);setMessage('工作偏好已保存，后续 PM 计划、判断和复盘将使用此版本。');
  }catch(e){setMessage(String(e)+'；草稿已保留。可加载最新版本后重新编辑。')}finally{setBusy(false)}}
  async function reload(){setBusy(true);try{const value=await api(prefix+'/pm/profile');setProfile(value);setDraft(value.preferences);setMessage('已加载最新版本。')}catch(e){setMessage(String(e))}finally{setBusy(false)}}
  return <section aria-label="PM 工作偏好"><h3>PM 工作偏好</h3>
    <p>明确告诉 PM 如何推进、何时求助、怎样汇报。模型从历史推断的偏好留在知识库，不会覆盖这里的设置。</p>
    {profile&&draft&&<form onSubmit={e=>{e.preventDefault();void save()}}>
      <p>{profile.source==='owner'?'用户明确设置':'默认工作原则'} · 版本 {profile.revision}</p>
      {(Object.keys(labels) as (keyof Preferences)[]).map(key=><label key={key}>{labels[key]}<textarea maxLength={2000} required value={draft[key]} onChange={e=>setDraft({...draft,[key]:e.target.value})}/></label>)}
      <button disabled={busy}>保存工作偏好</button><button type="button" disabled={busy} onClick={()=>void reload()}>加载最新偏好（替换草稿）</button>
    </form>}{message&&<p role="status">{message}</p>}
  </section>;
}
