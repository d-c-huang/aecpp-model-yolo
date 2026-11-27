"""Disposable, content-addressed analysis checkpoints; never learning evidence."""
import hashlib
import json
from pathlib import Path
import re
import uuid

from video_semantics import _check,_request,validate_base_url

HEX=re.compile(r'[a-f0-9]{64}')
MAX_BYTES=2*1024*1024


def signature(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':')).encode('utf-8')).hexdigest()


def file_hash(path,stop=None):
    path=Path(path);before=path.stat();digest=hashlib.sha256()
    with path.open('rb') as stream:
        while block:=stream.read(8*1024*1024):_check(stop);digest.update(block)
    _check(stop)
    if (before.st_size,before.st_mtime_ns)!=(path.stat().st_size,path.stat().st_mtime_ns):raise ValueError('原片在缓存校验期间变化，请重试')
    return digest.hexdigest()


class AnalysisCache:
    def __init__(self,root):self.root=Path(root).resolve()

    def get(self,key):
        if not HEX.fullmatch(key):return None
        path=self.root/(key+'.json')
        try:
            if path.resolve().parent!=self.root or path.stat().st_size>MAX_BYTES:return None
            row=json.loads(path.read_text(encoding='utf-8'))
            if row.get('version')!=1 or row.get('key')!=key or row.get('sha256')!=signature(row['value']):return None
            return row['value']
        except (OSError,ValueError,KeyError,TypeError,RecursionError):return None

    def put(self,key,value):
        if not HEX.fullmatch(key):return False
        raw=json.dumps({'version':1,'key':key,'sha256':signature(value),'value':value},ensure_ascii=False,allow_nan=False).encode('utf-8')
        if len(raw)>MAX_BYTES:return False
        self.root.mkdir(parents=True,exist_ok=True)
        temporary=self.root/(key+'.'+uuid.uuid4().hex+'.tmp')
        try:
            temporary.write_bytes(raw);temporary.replace(self.root/(key+'.json'))
            self.prune()
            return True
        finally:temporary.unlink(missing_ok=True)

    def prune(self):
        # Only generated flat hash-named files; no recursive deletion or source
        # frames are involved. Eviction may cost inference, never correctness.
        files=[]
        for path in self.root.glob('*.json'):
            if HEX.fullmatch(path.stem) and path.resolve().parent==self.root:
                stat=path.stat();files.append((stat.st_mtime,path,stat.st_size))
        files.sort(reverse=True);total=0
        for index,(_,path,size) in enumerate(files):
            total+=size
            if index>=4000 or total>256*1024*1024:path.unlink(missing_ok=True)


def ollama_identity(model,base_url,*,api_key='',report=lambda event:None,stop=None):
    value=_request(base_url+'/api/tags',api_key=api_key,timeout=10,stop=stop,report=report)
    names={model,model+':latest' if ':' not in model else model}
    rows=value.get('models',[])
    if not isinstance(rows,list):return None
    for row in rows:
        if not isinstance(row,dict) or not names.intersection({row.get('name'),row.get('model')}):continue
        digest=str(row.get('digest','')).removeprefix('sha256:')
        if HEX.fullmatch(digest):return {'model':model,'base_url':base_url,'digest':digest}
    return None


class Checkpoints:
    def __init__(self,folder,model,provider,base_url,*,api_key='',report=lambda event:None,stop=None):
        self.cache=AnalysisCache(folder) if folder and provider=='ollama' else None
        self.identity=None;self.model,self.base_url=model,validate_base_url(base_url)
        self.api_key,self.report,self.stop=api_key,report,stop
        self.hits=0;self.requests=0;self.load_seconds=0.;self.inference_seconds=0.
        if self.cache:
            try:self.identity=self._identity()
            except (OSError,ValueError,TypeError):_check(stop)
            if not self.identity:
                self.cache=None;report({'phase':'analysis-cache','message':'无法确认模型版本，本次直接分析，不复用推理缓存'})

    def _identity(self):return ollama_identity(self.model,self.base_url,api_key=self.api_key,stop=self.stop)

    def key(self,kind,payload,source_hash=''):
        if not self.cache:return None
        return signature({'kind':kind,'identity':self.identity,'source_hash':source_hash,
                          'request':{k:v for k,v in payload.items() if k!='keep_alive'}})

    def get(self,key,parse):
        if not key:return None
        raw=self.cache.get(key)
        if raw is not None:
            try:
                parsed=parse(raw);self.hits+=1;return parsed
            except (ValueError,KeyError,TypeError):pass
        return None

    def verify(self):
        _check(self.stop)
        if self.cache and self._identity()!=self.identity:raise ValueError('模型版本在分析期间变化，请重新分析')

    def put(self,key,raw):
        if not key:return
        self.verify()
        try:self.cache.put(key,raw)
        except (OSError,ValueError):
            self.report({'phase':'analysis-cache','message':'缓存暂未保存；本次分析结果照常保留'})

    def record_request(self,raw,elapsed):
        self.requests+=1;self.inference_seconds+=elapsed
        value=raw.get('load_duration')
        if type(value) in (int,float) and 0<=value<10**15:self.load_seconds+=value/10**9

    def summary(self):
        return {'cache_hits':self.hits,'model_requests':self.requests,'model_load_seconds':round(self.load_seconds,3),
                'request_seconds':round(self.inference_seconds,3),'model_digest':(self.identity or {}).get('digest','')}
