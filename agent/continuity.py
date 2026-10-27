"""Deterministic navigation summaries, derived from the current effective ledger."""
from .knowledge_versions import content_version
from .statements import effective_analysis, prompt_statement_context

CONTEXT_POLICY_VERSION = 'effective-records-recent-24k-v1'
RECENT_CONTEXT_CHARS = 24_000


def resume_summary(metadata):
    if not isinstance(metadata,dict) or metadata.get('mode','consult')!='consult':return None
    analysis=effective_analysis(metadata) or {}
    records=prompt_statement_context(metadata)['active']
    # Unsourced assistant wording cannot silently become a remembered user fact.
    sourced=[r for r in records if r.get('source_excerpt') or r.get('confirmation')=='confirmed']
    goals=[r for r in sourced if r['field']=='stage_goal']
    facts=[r for r in sourced if r['field'].split(':',1)[0] in {'known_facts','constraints'}][-4:]
    actions=[{k:a[k] for k in ('id','title','status','feedback','source_turn_id') if k in a}
             for a in metadata.get('actions',[]) if isinstance(a,dict) and a.get('status') in {'accepted','tried','reviewed','stopped'}][-3:]
    unknowns=[x for x in analysis.get('uncertainties',[]) if isinstance(x,str)][:3]
    if not goals and not facts and not actions and not unknowns:return None
    result={'policy_version':CONTEXT_POLICY_VERSION,'goal':goals[-1] if goals else None,
            'facts':facts,'actions':actions,'unknowns':unknowns,
            'notice':'由当前有效记录整理，可随时纠正；准备尝试不代表已经做过。'}
    result['version']=content_version(result)
    return result
