import json

import pytest

from token_counter.accounting import SSEParser, Usage, metrics


@pytest.mark.parametrize("p,c,limit,remaining,overflow",[(7754,3260,204800,193786,0),(7754,3260,1048576,1037562,0),(200000,4800,204800,0,0),(200000,5000,204800,0,200)])
def test_metrics(p,c,limit,remaining,overflow):
    result=metrics(dict(prompt_tokens=p,completion_tokens=c,context_limit=limit,context_limit_status="unverified"))
    assert result["occupied_tokens"]==p+c
    assert result["remaining_after_response"]==remaining
    assert result["overflow_tokens"]==overflow
    assert result["utilization_percent"]==100*(p+c)/limit


@pytest.mark.parametrize("limit,status",[(None,"unknown"),(204800,"conflict"),(204800,"unknown")])
def test_unknown_limits(limit,status):
    result=metrics(dict(prompt_tokens=10,completion_tokens=2,context_limit=limit,context_limit_status=status))
    assert result["occupied_tokens"]==12
    assert result["remaining_after_response"] is None


@pytest.mark.parametrize("usage,status,p,c,t",[(None,"unknown",None,None,None),({},"unknown",None,None,None),({"prompt_tokens":0,"completion_tokens":0},"complete",0,0,0),({"prompt_tokens":10},"partial",10,None,None),({"prompt_tokens":10,"completion_tokens":5},"complete",10,5,15),({"prompt_tokens":True,"completion_tokens":-1},"unknown",None,None,None)])
def test_usage_null_zero_and_derived(usage,status,p,c,t):
    u=Usage();u.observe({"usage":usage});event={"request_status":"completed"};u.apply(event)
    assert (event["usage_status"],event["prompt_tokens"],event["completion_tokens"],event["total_tokens"])==(status,p,c,t)


@pytest.mark.parametrize("stride",[1,2,7,67,1000])
def test_sse_boundaries_usage_and_reasoning(stride):
    data=(": comment\r\n\r\ndata: "+json.dumps({"choices":[{"delta":{"content":"Привет 🔧","reasoning_content":"secret reasoning"},"finish_reason":"tool_calls"}]},ensure_ascii=False)+"\r\n\r\n")
    usage={"prompt_tokens":100,"completion_tokens":30,"total_tokens":130,"completion_tokens_details":{"reasoning_tokens":10},"prompt_tokens_details":{"cached_tokens":50}}
    data+=("data: "+json.dumps({"choices":[],"usage":usage})+"\n\n")*2+"data: [DONE]\n\n"
    raw=data.encode();u=Usage();parser=SSEParser(u)
    for start in range(0,len(raw),stride):parser.feed(raw[start:start+stride])
    parser.finish();event={"request_status":"completed"};u.apply(event)
    assert u.done and event["total_tokens"]==130 and event["completion_tokens"]==30
    assert event["usage_details"]["prompt_tokens_details.cached_tokens"]==50
    assert event["finish_reason"]=="tool_calls" and len(event["usage_observations"])==1
    assert "secret reasoning" not in json.dumps(event)


def test_conflicts_partial_and_bounded_sse():
    u=Usage();u.observe({"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":99}})
    e={"request_status":"error"};u.apply(e)
    assert e["usage_status"]=="partial" and e["total_tokens"]==99 and "total_mismatch" in e["flags"]
    parser=SSEParser(u,max_event=20);parser.feed(b"data: "+b"a"*100);parser.feed(b"\n\ndata: [DONE]\n\n")
    assert u.done and len(parser.buffer)<=20 and "sse_event_too_large" in u.flags
