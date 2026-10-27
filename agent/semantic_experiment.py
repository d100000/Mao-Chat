"""Opt-in local retrieval experiment. Never used to enable production retrieval.

The caller supplies an already installed model directory. No model download,
remote embedding request, shared user memory or historical-mode path exists.
"""
import importlib.util
import math
from pathlib import Path
from .principles import available_principles, search_principles
from .vector import rrf_weight


def semantic_diagnostics():
    installed=importlib.util.find_spec('sentence_transformers') is not None
    return {'enabled':False,'status':'disabled','dependency_installed':installed,
            'reason':('尚无通过独立评测的本地语义模型' if installed else '语义依赖未安装且无已验收模型')+'，继续使用增强词法'}


class LocalSemanticExperiment:
    def __init__(self, model_dir=None, *, loader=None, minimum_similarity=.65):
        self.model_dir=Path(model_dir) if model_dir is not None else None
        self.loader=loader
        self.minimum_similarity=float(minimum_similarity)
        if not math.isfinite(self.minimum_similarity) or not -1<=self.minimum_similarity<=1:raise ValueError('invalid similarity threshold')
        self.model=None;self.status='disabled' if self.model_dir is None else 'not_loaded'

    def _load(self):
        if self.status!='not_loaded':return
        if not self.model_dir.is_dir():self.status='model_missing';return
        try:
            if self.loader is None:
                from sentence_transformers import SentenceTransformer
                self.loader=SentenceTransformer
            self.model=self.loader(str(self.model_dir.resolve()),device='cpu',local_files_only=True,
                                   trust_remote_code=False,token=False,model_kwargs={'use_safetensors':True})
            self.status='ready'
        except Exception:
            self.status='load_failed';self.model=None

    def search(self, query, reviewed_ids):
        # Filter support qualification before passing any candidate to the encoder.
        rows=available_principles(reviewed_ids)
        lexical=search_principles(query,reviewed_ids,enhanced=True)
        self._load()
        if self.model is None or not rows:return lexical
        try:
            texts=[' '.join([r['label'],r['research_interpretation'],r['modern_application'],*r['query_examples']]) for r in rows]
            vectors=self.model.encode([query,*texts],normalize_embeddings=True,show_progress_bar=False)
            if len(vectors)!=len(rows)+1:raise ValueError('shape')
            query_vector=[float(x) for x in vectors[0]]
            if not 1<=len(query_vector)<=4096 or not all(math.isfinite(x) for x in query_vector):raise ValueError('dimension')
            ranked=[]
            for row,vector in zip(rows,vectors[1:]):
                values=[float(x) for x in vector]
                if len(values)!=len(query_vector) or not all(math.isfinite(x) for x in values):raise ValueError('vector')
                score=sum(a*b for a,b in zip(query_vector,values))
                if score>=self.minimum_similarity:ranked.append((score,row))
            semantic=[r for _,r in sorted(ranked,key=lambda x:(-x[0],x[1]['principle_id']))[:3]]
            fused={}
            for channel in (lexical,semantic):
                for rank,row in enumerate(channel,1):
                    key=row['principle_id'];entry=fused.setdefault(key,{**row,'hybrid_score':0.0})
                    entry['hybrid_score']+=rrf_weight(rank)
            return sorted(fused.values(),key=lambda x:(-x['hybrid_score'],x['principle_id']))[:3]
        except Exception:
            self.status='encoding_failed';self.model=None
            return lexical
