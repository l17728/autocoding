import {useEffect, useState} from 'react';

function assetsFromHtml(html:string) {
  const page=new DOMParser().parseFromString(html,'text/html');
  return Array.from(page.querySelectorAll<HTMLScriptElement | HTMLLinkElement>('script[src],link[rel="stylesheet"][href]'))
    .map(element=>element instanceof HTMLScriptElement?element.src:(element as HTMLLinkElement).href)
    .filter(url=>new URL(url,window.location.href).origin===window.location.origin)
    .map(url=>new URL(url).pathname)
    .filter(path=>path.startsWith('/assets/'))
    .sort()
    .join('\n');
}

function currentAssets() {
  return Array.from(document.querySelectorAll<HTMLScriptElement | HTMLLinkElement>('script[src],link[rel="stylesheet"][href]'))
    .map(element=>element instanceof HTMLScriptElement?element.src:(element as HTMLLinkElement).href)
    .filter(url=>new URL(url,window.location.href).origin===window.location.origin)
    .map(url=>new URL(url).pathname)
    .filter(path=>path.startsWith('/assets/'))
    .sort()
    .join('\n');
}

function hasPendingInput() {
  if(document.querySelector('[role="dialog"]'))return true;
  const active=document.activeElement;
  if(active instanceof HTMLElement && (active.isContentEditable || active.matches('input:not([type="button"]):not([type="submit"]),textarea')))
    return true;
  return Array.from(document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>('input:not([type="button"]):not([type="submit"]):not([type="file"]),textarea,select'))
    .some(field=>{
      if(field instanceof HTMLSelectElement)return Array.from(field.selectedOptions).some(option=>!option.defaultSelected);
      if(field instanceof HTMLInputElement && ['checkbox','radio'].includes(field.type))return field.checked!==field.defaultChecked;
      return field.value.trim()!==field.defaultValue.trim();
    });
}

export function UpdateMonitor({busy=false}:{busy?:boolean}) {
  const [available,setAvailable]=useState(false);

  useEffect(()=>{
    let active=true;
    let checking=false;
    let timer:ReturnType<typeof setTimeout>;
    const loadedAssets=currentAssets();
    const check=async()=>{
      if(checking)return;
      checking=true;
      try {
        const response=await fetch('/',{cache:'no-store',headers:{'X-UI-Version-Check':'1'}});
        if(!response.ok)throw new Error(`HTTP ${response.status}`);
        const html=await response.text();
        if(active && assetsFromHtml(html)!==loadedAssets)setAvailable(true);
      } catch {
        // The regular connection indicator owns service availability errors.
      } finally {
        checking=false;
        if(active)timer=setTimeout(check,30000);
      }
    };
    const onVisible=()=>{if(document.visibilityState==='visible'){clearTimeout(timer);void check()}};
    timer=setTimeout(check,30000);
    document.addEventListener('visibilitychange',onVisible);
    return()=>{active=false;clearTimeout(timer);document.removeEventListener('visibilitychange',onVisible)};
  },[]);

  useEffect(()=>{
    if(!available || busy || document.visibilityState!=='visible')return;
    const timer=setInterval(()=>{
      if(!busy && document.visibilityState==='visible' && !hasPendingInput())window.location.reload();
    },1500);
    return()=>clearInterval(timer);
  },[available,busy]);

  if(!available)return null;
  return <div className="update-notice" role="status" aria-live="polite">
    <span>平台已更新，正在安全同步最新界面。</span>
    <button type="button" onClick={()=>{
      if((busy || hasPendingInput()) && !window.confirm('页面有尚未提交的内容，刷新可能丢失这些输入。仍要刷新吗？'))return;
      window.location.reload();
    }}>立即刷新</button>
  </div>;
}
