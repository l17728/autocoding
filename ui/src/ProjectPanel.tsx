import {useEffect,useState} from 'react';
type Project={id:string;name:string;root:string;github_owner?:string;github_repository?:string;permission_mode?:string;revision?:number};
export type Api=(path:string,body?:unknown)=>Promise<any>;
const blank={name:'',root:'',github_owner:'',github_repository:'',permission_mode:'bypass',initialize_git:false};
export function ProjectPanel({project,api,select,disabled=false}:{project:Project;api:Api;select:(p:Project)=>void;disabled?:boolean}) {
  const [projects,setProjects]=useState<Project[]>([]),[settings,setSettings]=useState<Project>(project),[draft,setDraft]=useState(blank);
  const [message,setMessage]=useState(''),[busy,setBusy]=useState(false),[directory,setDirectory]=useState<any>(null);
  const prefix=`/api/v1/projects/${project.id}`;
  useEffect(()=>{let active=true;setSettings(project);setMessage('');setDirectory(null);
    Promise.all([api('/api/v1/projects'),api(prefix+'/settings'),api(prefix+'/directory')]).then(([list,detail,info])=>{
      if(active){setProjects(list);setSettings(detail);setDirectory(info)}
    }).catch(e=>{if(active)setMessage(String(e))});return()=>{active=false};
  },[project.id]);
  async function action(work:()=>Promise<void>){setBusy(true);setMessage('');try{await work()}catch(e){setMessage(String(e))}finally{setBusy(false)}}
  return <details className="panel project-panel"><summary>项目与仓库设置 · {project.name}</summary><fieldset disabled={disabled} style={{border:0,padding:0,margin:0,minWidth:0}}>
    <label>切换项目<select aria-label="切换项目" value={project.id} disabled={busy} onChange={e=>{const p=projects.find(p=>p.id===e.target.value);if(p)select(p)}}>{projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
    <form onSubmit={e=>{e.preventDefault();void action(async()=>{const saved=await api(prefix+'/settings',{
      name:settings.name,github_owner:settings.github_owner||'',github_repository:settings.github_repository||'',permission_mode:settings.permission_mode||'bypass',revision:settings.revision||1});
      setSettings(saved);setProjects(await api('/api/v1/projects'));select(saved);setMessage('已保存，权限策略用于新启动的会话。')})}}>
      <h2>当前项目</h2><div className="settings-grid">
      <label>项目名称<input required value={settings.name} onChange={e=>setSettings({...settings,name:e.target.value})}/></label>
      <label>本地项目目录<input readOnly value={settings.root}/></label>
      <label>GitHub 用户名或组织<input value={settings.github_owner||''} onChange={e=>setSettings({...settings,github_owner:e.target.value})}/></label>
      <label>GitHub 仓库名<input value={settings.github_repository||''} onChange={e=>setSettings({...settings,github_repository:e.target.value})}/></label>
      <label>默认执行权限<select value={settings.permission_mode||'bypass'} onChange={e=>setSettings({...settings,permission_mode:e.target.value})}><option value="bypass">完整访问 · 跳过权限审批</option><option value="review">完整访问 · PM 审批</option></select></label></div>
      {settings.github_owner&&settings.github_repository&&<p><a target="_blank" rel="noreferrer" href={`https://github.com/${encodeURIComponent(settings.github_owner)}/${encodeURIComponent(settings.github_repository)}`}>查看 GitHub 仓库 ↗</a></p>}
      {directory&&<p className="muted">{directory.can_execute?'已有提交，可从当前版本增量开发。':directory.dirty?'存在未提交修改，请先完成本地提交再执行开发。':'尚无干净的 Git 提交基线，可以先制定计划。'} {directory.head&&`当前提交 ${directory.head.slice(0,12)}`}</p>}
      <p className="muted">目录绑定后不移动文件；更换目录请接入为另一个项目。GitHub 信息仅保存关联，不自动创建仓库或推送。</p>
      <button disabled={busy}>保存项目设置</button>
    </form>
    <details><summary>创建或接入本地项目</summary><form onSubmit={e=>{e.preventDefault();void action(async()=>{
      const created=await api('/api/v1/projects',draft);setProjects(await api('/api/v1/projects'));setDraft(blank);select(created);
    })}}><div className="settings-grid">
      <label>新项目名称<input required value={draft.name} onChange={e=>setDraft({...draft,name:e.target.value})}/></label>
      <label>本地目录（绝对路径）<input required value={draft.root} placeholder="D:\projects\my-app" onChange={e=>setDraft({...draft,root:e.target.value})}/></label>
      <label>新项目 GitHub 用户名或组织<input value={draft.github_owner} onChange={e=>setDraft({...draft,github_owner:e.target.value})}/></label>
      <label>新项目 GitHub 仓库名<input value={draft.github_repository} onChange={e=>setDraft({...draft,github_repository:e.target.value})}/></label>
      </div><label className="check-label"><input type="checkbox" checked={draft.initialize_git} onChange={e=>setDraft({...draft,initialize_git:e.target.checked})}/>为新空目录初始化 Git 和空提交基线</label>
      <button type="button" disabled={busy||!draft.root} onClick={()=>void action(async()=>{const p=await api('/api/v1/projects/inspect',{root:draft.root});setMessage(`${p.exists?'目录已存在':'将创建目录'} · ${p.empty?'空目录':p.git?'已有 Git 仓库':'已有文件'}${p.dirty?' · 有未提交修改':''}`)})}>检查目录</button>
      <button className="primary" disabled={busy}>创建或接入项目</button>
      <p className="muted">已有项目保留文件与提交历史；非空目录不会自动初始化或批量提交文件。</p>
    </form></details>{message&&<p role="status">{message}</p>}
  </fieldset></details>
}
