import {useEffect,useState} from 'react';
type Project={id:string;name:string;root:string;github_owner?:string;github_repository?:string;permission_mode?:string;revision?:number};
export type Api=(path:string,body?:unknown,idempotencyKey?:string)=>Promise<any>;
export function ProjectPanel({project,api,select,disabled=false}:{project:Project;api:Api;select:(p:Project)=>void;disabled?:boolean}) {
  const [projects,setProjects]=useState<Project[]>([]),[settings,setSettings]=useState<Project>(project);
  const [message,setMessage]=useState(''),[busy,setBusy]=useState(false),[directory,setDirectory]=useState<any>(null),[documents,setDocuments]=useState<{name:string;bytes:number}[]>([]),[deleteName,setDeleteName]=useState('');
  const prefix=`/api/v1/projects/${project.id}`;
  useEffect(()=>{let active=true;setSettings(project);setMessage('');setDirectory(null);
    Promise.all([api('/api/v1/projects'),api(prefix+'/settings'),api(prefix+'/directory'),api(prefix+'/design-documents')]).then(([list,detail,info,docs])=>{
      if(active){setProjects(list);setSettings(detail);setDirectory(info);setDocuments(docs)}
    }).catch(e=>{if(active)setMessage(String(e))});return()=>{active=false};
  },[project.id]);
  async function action(work:()=>Promise<void>){setBusy(true);setMessage('');try{await work()}catch(e){setMessage(String(e))}finally{setBusy(false)}}
  return <details className="panel project-panel"><summary>项目与仓库设置 · {project.name}</summary><fieldset disabled={disabled} style={{border:0,padding:0,margin:0,minWidth:0}}>
    <label>切换项目<select aria-label="切换项目" value={project.id} disabled={busy} onChange={e=>{const p=projects.find(p=>p.id===e.target.value);if(p)select(p)}}>{projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
    <form onSubmit={e=>{e.preventDefault();void action(async()=>{const root=settings.root.trim();if(root!==project.root){const info=await api('/api/v1/projects/inspect',{root});if(info.registered_project){select(info.registered_project);setMessage(`已切换到项目“${info.registered_project.name}”。`);return}const attached=await api('/api/v1/projects',{name:settings.name,root,github_owner:settings.github_owner||'',github_repository:settings.github_repository||'',permission_mode:settings.permission_mode||'bypass'});setProjects(await api('/api/v1/projects'));select(attached);setMessage(`已接入目录：${attached.root}`);return}const saved=await api(prefix+'/settings',{
      name:settings.name,github_owner:settings.github_owner||'',github_repository:settings.github_repository||'',permission_mode:settings.permission_mode||'bypass',revision:settings.revision||1});
      setSettings(saved);setProjects(await api('/api/v1/projects'));select(saved);setMessage('已保存，权限策略用于新启动的会话。')})}}>
      <h2>当前项目</h2><div className="settings-grid">
      <label>项目名称<input required value={settings.name} onChange={e=>setSettings({...settings,name:e.target.value})}/></label>
      <label>本地项目目录<input required value={settings.root} onChange={e=>setSettings({...settings,root:e.target.value})}/></label>
      <label>GitHub 用户名或组织<input value={settings.github_owner||''} onChange={e=>setSettings({...settings,github_owner:e.target.value})}/></label>
      <label>GitHub 仓库名<input value={settings.github_repository||''} onChange={e=>setSettings({...settings,github_repository:e.target.value})}/></label>
      <label>默认执行权限<select value={settings.permission_mode||'bypass'} onChange={e=>setSettings({...settings,permission_mode:e.target.value})}><option value="bypass">完整访问 · 跳过权限审批</option><option value="review">完整访问 · PM 审批</option></select></label></div>
      {settings.github_owner&&settings.github_repository&&<p><a target="_blank" rel="noreferrer" href={`https://github.com/${encodeURIComponent(settings.github_owner)}/${encodeURIComponent(settings.github_repository)}`}>查看 GitHub 仓库 ↗</a></p>}
      {directory&&<p className="muted">{directory.can_execute?'已有提交，可从当前版本增量开发。':directory.dirty?'存在未提交修改，请先完成本地提交再执行开发。':'尚无干净的 Git 提交基线，可以先制定计划。'} {directory.head&&`当前提交 ${directory.head.slice(0,12)}`}</p>}
      <p className="muted">在这里输入新目录并保存即可检查并接入：已登记目录会直接切换，未登记目录会保留已有文件和提交历史后接入。GitHub 信息仅保存关联，不自动创建仓库或推送。</p>
      <button disabled={busy}>{settings.root.trim()!==project.root?'保存并接入目录':'保存项目设置'}</button>
    </form>
    {project.id!==projects[0]?.id&&<details><summary>删除项目</summary><p className="muted">此操作会永久删除登记信息和本地目录全部内容。输入项目名称“{settings.name}”确认。</p><label>确认项目名称<input value={deleteName} onChange={e=>setDeleteName(e.target.value)}/></label><button type="button" disabled={busy||deleteName!==settings.name} onClick={()=>void action(async()=>{await api(prefix,{_method:'DELETE',confirmation:deleteName});const list=await api('/api/v1/projects');setProjects(list);select(list[0])})}>永久删除项目和本地目录</button></details>}
    <details><summary>导入设计文档</summary><p className="muted">可一次选择多个 UTF-8 Markdown 或文本文件，也可稍后继续追加；单个文件最大 512 KiB。文件保存至项目的 <code>docs/imported</code>，可作为 PM 规划和开发上下文。</p>
      <label>设计文档<input aria-label="设计文档" type="file" multiple accept=".md,.txt,text/markdown,text/plain" disabled={busy} onChange={e=>{const input=e.currentTarget;const files=Array.from(input.files||[]);if(!files.length)return;void action(async()=>{try{for(const file of files){if(file.size>512*1024)throw new Error(`设计文档“${file.name}”超过 512 KiB。`);const bytes=new Uint8Array(await file.arrayBuffer());let binary='';bytes.forEach(value=>binary+=String.fromCharCode(value));await api(prefix+'/design-documents',{filename:file.name,content_base64:btoa(binary)})}setDocuments(await api(prefix+'/design-documents'));setMessage(`已导入 ${files.length} 个设计文档。`)}finally{input.value=''}})}}/></label>
      {documents.length>0&&<ul>{documents.map(document=><li key={document.name}>{document.name}（{document.bytes} 字节） <button type="button" disabled={busy} onClick={()=>void action(async()=>{await api(prefix+'/design-documents/'+encodeURIComponent(document.name),{_method:'DELETE'});setDocuments(await api(prefix+'/design-documents'));setMessage(`已删除设计文档：${document.name}`)})}>删除</button></li>)}</ul>}
    </details>
    {message&&<p role="status">{message}</p>}
  </fieldset></details>
}
