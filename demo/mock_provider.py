"""Deterministic OpenAI-compatible model fixture; no weights or external API."""
import json
import time
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

app = FastAPI()
MODELS = {'demo-small': (120, 30, 12), 'demo-large': (240, 60, 24)}


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.get('/v1/models')
def models():
    return {'object': 'list', 'data': [dict(id=m, object='model', created=0, owned_by='demo') for m in MODELS]}


@app.post('/api/show')
def show():
    return {'capabilities': ['completion'], 'details': {'family': 'demo'},
            'model_info': {'general.architecture': 'demo', 'demo.context_length': 4096}}


@app.post('/v1/chat/completions')
def chat(body: dict):
    model = body.get('model')
    if model not in MODELS:
        raise HTTPException(404, 'unknown demo model')
    prompt, cached, output = MODELS[model]
    usage = dict(prompt_tokens=prompt, completion_tokens=output, total_tokens=prompt+output,
                 prompt_tokens_details={'cached_tokens': cached})
    common = dict(id='chatcmpl-'+uuid.uuid4().hex, created=int(time.time()), model=model)
    if not body.get('stream'):
        return dict(**common, object='chat.completion', usage=usage,
                    choices=[dict(index=0, message=dict(role='assistant', content='Hello.'), finish_reason='stop')])

    def chunks():
        for delta, finish in [({'role': 'assistant', 'content': 'Hello.'}, None), ({}, 'stop')]:
            yield 'data: '+json.dumps(dict(**common, object='chat.completion.chunk',
                choices=[dict(index=0, delta=delta, finish_reason=finish)]))+'\n\n'
        yield 'data: '+json.dumps(dict(**common, object='chat.completion.chunk', choices=[], usage=usage))+'\n\n'
        yield 'data: [DONE]\n\n'
    return StreamingResponse(chunks(), media_type='text/event-stream')
