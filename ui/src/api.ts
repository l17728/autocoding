// Keep failures useful even when a proxy or a disconnected service returns HTML.
export async function readResponse(response:Response) {
  let data:any;
  try { data=await response.json(); } catch {
    throw new Error(`服务响应无法读取（HTTP ${response.status}），请检查本机服务并核对操作结果，勿重复提交。`);
  }
  if(!response.ok){
    const code=data.error?.code||`HTTP ${response.status}`;
    const descriptions:Record<string,string>={UNAUTHORIZED:'访问令牌无效或已过期，请刷新页面重新连接。',INVALID_REQUEST:'提交内容不符合要求，请检查表单。',PROJECT_REVISION_CONFLICT:'项目设置已被修改，请重新加载后再保存。',STORAGE_BUSY:'项目存储繁忙，请稍后刷新并核对操作结果；系统不会自动重发。',STORAGE_UNAVAILABLE:'项目存储暂不可用，请检查磁盘空间和目录权限，恢复后先核对操作结果。'};
    Object.assign(descriptions,{
      TERMINAL_INPUT_UNCONFIRMED:'终端输入结果无法确认，已停止本次会话；请核对记录，不要重复发送。',
      EDITOR_INPUT_UNCONFIRMED:'草稿交接结果无法确认，已停止本次会话；请核对记录，不要重复发送。',
      EDITOR_HANDOFF_TIMEOUT:'等待终端接收草稿超时，已停止本次会话；请先核对输入。',
      EDITOR_DRAFT_REJECTED:'终端已有未提交草稿，未覆盖；请核对原草稿后再继续。',
      EDITOR_READBACK_MISMATCH:'终端草稿读回内容不一致，未提交并已停止本次会话；请核对记录，不要重复发送。',
      CONSOLE_INPUT_FAILED:'本机输入通道失败，已停止本次会话；请先核对输入结果。',
      STALE_PM_PROFILE:'工作偏好已被修改；请先加载最新偏好，再决定如何合并草稿。',
      INVALID_PM_PROFILE:'工作偏好必须填写完整，每项不超过 2000 字。',
      DELEGATION_REQUIRES_REGISTERED_CHECKS:'委托执行需要项目已登记的独立检查名称；请先配置验收检查。',
      DELEGATION_ACCEPTANCE_GAP:'PM 计划未完整覆盖指定验收，已停止采用；请检查计划与验收要求。',
    });
    if(code.startsWith('PROJECT_CLOSURE_'))throw new Error(`尚不能生成收尾报告：请确认全部登记任务完成、无未解决交互且最新批次集成验收通过。（${code}）`);
    throw new Error(descriptions[code]||`操作未完成：${code}${data.error?.trace_id?`（记录 ${data.error.trace_id}）`:''}`);
  }
  return data;
}
