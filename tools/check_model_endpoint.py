"""One small Anthropic-compatible request; report status only, never credentials/body."""
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.providers import resolve_zhipu


def main():
    report={'key_printed':False,'response_body_printed':False,'passed':False}
    try:
        config=resolve_zhipu(ROOT/'.local/credentials/opencode.json')
        request=urllib.request.Request('https://open.bigmodel.cn/api/anthropic/v1/messages',
            data=json.dumps({'model':config.model,'max_tokens':128,'messages':[{'role':'user','content':'Reply OK.'}]}).encode(),
            headers={'Content-Type':'application/json','anthropic-version':'2023-06-01','x-api-key':config.api_key})
        # Refuse redirects so authentication never crosses to a different host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):
                return None
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=45) as response:
            body=json.loads(response.read(65536))
            report.update(http_status=response.status,passed=body.get('type')=='message')
    except urllib.error.HTTPError as exc:
        report.update(http_status=exc.code,error='HTTP_REJECTED')
        # Extract only bounded numeric codes and classify known messages in memory.
        # Never persist response text, request IDs, or arbitrary provider strings.
        try:
            body=json.loads(exc.read(65536))
            error=body.get('error',{})
            code=str(error.get('code','')) if isinstance(error,dict) else ''
            if code.isdecimal() and len(code)<=8:
                report['provider_code']=code
            message=str(error.get('message','')).casefold() if isinstance(error,dict) else ''
            report['restriction']=('quota' if code in {'1304','1308','1310'} or any(word in message for word in ('余额','额度','套餐','quota','balance','套餐用量','使用上限'))
                                   else 'rate_limit' if any(word in message for word in ('频率','并发','rate limit','too many')) else 'unknown')
            retry=exc.headers.get('Retry-After','')
            if retry.isdecimal() and len(retry)<=8:
                report['retry_after_seconds']=int(retry)
        except (ValueError,TypeError,AttributeError):
            pass
        exc.close()
    except Exception as exc:
        report['error']=type(exc).__name__
    output=ROOT/'.local/model-endpoint-check.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
