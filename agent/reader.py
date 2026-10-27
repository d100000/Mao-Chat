"""Read only the qualified scope of a stable citation; bind questions to a version."""
import re
from urllib.parse import urlparse
from .dialogue import CuratedMethodLibrary
from .issues import IssueError
from .knowledge_versions import content_version, knowledge_revision


def read_source(db_path, source_id, version=None):
    if not isinstance(source_id, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_:-]{0,159}',source_id):
        raise IssueError('资料ID无效')
    before = knowledge_revision(db_path)
    result = CuratedMethodLibrary(db_path).verify_citations([source_id])
    rows = [r for k in ('results','principles','method_notes','cases') for r in result.get(k,[])]
    if not rows: raise IssueError('资料不存在或核验资格已撤回',404)
    row = rows[0]
    # A card is a bounded editorial scope, not the complete article.
    stable = {k:v for k,v in row.items() if not k.startswith('retrieval_') and k!='source_version'}
    revision = content_version(stable)
    if version is not None and version != revision:
        raise IssueError('资料版本已变化，请重新打开并选择片段；旧版本未作为当前依据提供',409)
    paragraphs=[]
    for field,label in [('excerpt','已核原文片段'),('summary','已核摘要'),('author_claim','研究者概括：原作主张'),
                        ('research_interpretation','研究者解释'),('historical_conditions','历史条件'),
                        ('modern_application','现代参考'),('unsupported_extensions','不支持的延伸'),
                        ('description','已核公开事件'),('decision_rule','方法概括'),('conditions','适用条件'),
                        ('modern_analogy','现代类比'),('non_analogies','不可类比范围'),('unknowns','资料缺口')]:
        value=row.get(field)
        if isinstance(value,list): value='；'.join(x for x in value if isinstance(x,str))
        if not isinstance(value,str) or not value.strip(): continue
        paragraphs.append({'id':field+':'+content_version(value)[:20], 'label':label,'text':value,
                           'quote_eligible':field=='excerpt' and row.get('quote_eligible') is True})
    if not paragraphs: raise IssueError('此资料暂未提供可阅读的文本范围',404)
    links=[]
    for item in row.get('source_urls',[])[:10]:
        url=item if isinstance(item,str) else item.get('url','') if isinstance(item,dict) else ''
        parsed=urlparse(url)
        if parsed.scheme in {'http','https'} and parsed.netloc and not parsed.username: links.append(url)
    if knowledge_revision(db_path)!=before: raise IssueError('资料正在更新，请重试',409)
    scope=row.get('review_scope') or row.get('notice') or '仅所列已核摘要与片段'
    if scope=='AI-assisted source/date verification, limited to this summary and selected excerpt; not human historical certification.':
        scope='借助AI核对来源与年代，仅覆盖此摘要和选定片段，并非人工历史鉴定'
    return {'source_id':source_id,'version':revision,'library_revision':before,
            'title':row.get('article_title') or row.get('label') or row.get('title') or source_id,
            'record_type':row.get('record_type') or row.get('verification_status'),
            'scope':scope,
            'full_text_available':False,'notice':'当前提供核验片段与解释，不是完整原篇；可通过公开来源继续核对。',
            'paragraphs':paragraphs,'source_urls':links,'anchors':row.get('anchors',[]),
            'supporting_ids':row.get('evidence_ids',[])}


def selected_context(db_path, selection, mode):
    if selection is None: return None
    if mode != 'consult': raise IssueError('选段提问目前仅用于对话解惑；历史模式不带入当代回查资料')
    if not isinstance(selection,dict) or set(selection)!={'source_id','version','paragraph_id'} or not all(isinstance(v,str) for v in selection.values()):
        raise IssueError('选段字段无效')
    source=read_source(db_path,selection['source_id'],selection['version'])
    part=next((p for p in source['paragraphs'] if p['id']==selection['paragraph_id']),None)
    if part is None: raise IssueError('选段不属于所选资料版本，请重新选择',409)
    return {'source_id':source['source_id'],'version':source['version'],'title':source['title'],
            'scope':source['scope'],'paragraph':part,
            'notice':'用户选择讨论的材料，不是用户经历或用户事实；解释层不是原话，核验资格不因选中改变。'}


def selection_ref(value):
    """Shape-only for saved references; reading them still requires live eligibility."""
    if not isinstance(value,dict) or set(value)!={'source_id','version','paragraph_id'}:return None
    patterns={'source_id':r'[A-Za-z][A-Za-z0-9_:-]{0,159}','version':r'[a-f0-9]{64}','paragraph_id':r'[a-z_]+:[a-f0-9]{20}'}
    if any(not isinstance(value[k],str) or not re.fullmatch(pattern,value[k]) for k,pattern in patterns.items()):return None
    return dict(value)
