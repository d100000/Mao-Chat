"""Small, human-reviewed card releases. Content, derived indexes and manifest commit together."""
import datetime
import json
import re
import uuid
from urllib.parse import urlparse
from .db import connect
from .index import build_indexes
from .ingest import _upsert_evidence
from .issues import IssueError
from .knowledge_versions import content_version, revision_in_transaction
from .principles import PRINCIPLES
from .semantic_experiment import semantic_diagnostics

_TABLES=('evidence_cards','quotes','events','quote_event_links')
_FIELDS={'article_title','topic','summary','excerpt','source_id','original_date_display',
         'earliest_content_date','earliest_wording_date','known_to_character_from','content_qualification','source_urls','pdf_pages'}


def _snapshot(conn):
    return {table:[dict(r) for r in conn.execute('SELECT * FROM '+table)] for table in _TABLES}


def _restore(conn, snapshot):
    for table in reversed(_TABLES):conn.execute('DELETE FROM '+table)
    for table in _TABLES:
        for row in snapshot[table]:
            keys=list(row)
            conn.execute('INSERT INTO '+table+'('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',[row[k] for k in keys])


class KnowledgeStore:
    def __init__(self, db_path):
        self.db_path=db_path
        conn=connect(db_path)
        try:
            conn.executescript('''CREATE TABLE IF NOT EXISTS knowledge_drafts(
                id TEXT PRIMARY KEY, base_revision TEXT NOT NULL, card_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'candidate', created_by INTEGER NOT NULL,
                reviewed_by INTEGER, review_reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS knowledge_releases(
                id TEXT PRIMARY KEY, draft_id TEXT UNIQUE, before_revision TEXT NOT NULL, after_revision TEXT NOT NULL,
                before_json TEXT NOT NULL, manifest_json TEXT NOT NULL, actor INTEGER NOT NULL,
                rollback_of TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);''')
        finally:conn.close()

    def overview(self):
        conn=connect(self.db_path)
        try:
            conn.execute('BEGIN')
            cards=[dict(r) for r in conn.execute('SELECT evidence_id,article_title,reviewed,quote_verified,source_id,earliest_content_date FROM evidence_cards ORDER BY evidence_id')]
            drafts=[{'id':r['id'],'status':r['status'],'source_id':json.loads(r['card_json'])['id'],'created_at':r['created_at']} for r in conn.execute('SELECT * FROM knowledge_drafts ORDER BY created_at DESC,id DESC LIMIT 30')]
            releases=[dict(r) for r in conn.execute('SELECT id,draft_id,before_revision,after_revision,manifest_json,rollback_of,created_at FROM knowledge_releases ORDER BY rowid DESC LIMIT 20')]
            for r in releases:r['manifest']=json.loads(r.pop('manifest_json'))
            return {'revision':revision_in_transaction(conn),'cards':cards,'drafts':drafts,'releases':releases,
                    'coverage':{'reviewed':sum(bool(r['reviewed']) for r in cards),'candidate':sum(not r['reviewed'] for r in cards),
                                'sources':len({r['source_id'] for r in cards if r['source_id']}),
                                'undated':sum(not r['earliest_content_date'] for r in cards),
                                'full_text':'核验卡提供有限片段；全文完整性未核验，不用字数替代质量'},
                    'semantic':semantic_diagnostics()}
        finally:conn.close()

    def card(self, source_id):
        conn=connect(self.db_path)
        try:
            row=conn.execute('SELECT * FROM evidence_cards WHERE evidence_id=?',(source_id,)).fetchone()
            if not row:raise IssueError('资料不存在',404)
            result={k:row[k] for k in _FIELDS-{'source_urls','pdf_pages'} if k in row.keys()}
            raw=json.loads(row['raw_json'])
            result['source_urls']=[x if isinstance(x,str) else x.get('url','') for x in json.loads(row['source_urls_json'])]
            result['pdf_pages']=raw.get('pdf_pages',[])
            return result
        finally:conn.close()

    def create(self, source_id, patch, actor):
        if not isinstance(source_id,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}',source_id): raise IssueError('资料ID无效')
        if not isinstance(patch,dict) or not patch or set(patch)-_FIELDS:raise IssueError('只接受核验卡的内容字段，资格必须另行人工核验')
        for key,value in patch.items():
            if key in {'source_urls','pdf_pages'}:continue
            if value is not None and (not isinstance(value,str) or len(value)>6000):raise IssueError('内容字段需为不超过6000字的文本')
        for key in ('earliest_content_date','earliest_wording_date','known_to_character_from'):
            if patch.get(key):
                try:
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',patch[key]):raise ValueError('date format')
                    datetime.datetime.strptime(patch[key],'%Y-%m-%d')
                except (ValueError,TypeError):raise IssueError('日期需为YYYY-MM-DD或留空') from None
        if 'source_urls' in patch:
            urls=patch['source_urls']
            if not isinstance(urls,list) or len(urls)>8 or any(not isinstance(u,str) or len(u)>2000 or urlparse(u).scheme not in {'https','http'} or not urlparse(u).netloc or urlparse(u).username for u in urls):raise IssueError('公开来源需为最多8个HTTP(S)链接')
        if 'pdf_pages' in patch and (not isinstance(patch['pdf_pages'],list) or len(patch['pdf_pages'])>12 or any(type(n)!=int or not 1<=n<=10000 for n in patch['pdf_pages'])):raise IssueError('页码格式无效')
        conn=connect(self.db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM evidence_cards WHERE evidence_id=?',(source_id,)).fetchone()
            if row and row['source_role']=='biographical_record':raise IssueError('首版编辑器仅维护原著核验卡；事件另行核验')
            card=json.loads(row['raw_json']) if row else {'id':source_id,'source_role':'author_work'}
            if row:
                card.update({k:row[k] for k in _FIELDS-{'source_urls','pdf_pages'} if k in row.keys()})
                card['source_urls']=json.loads(row['source_urls_json'])
            card.update(patch);card.update(id=source_id,reviewed=False,quote_verified=False)
            if row: card['interpretations_disabled']=True
            for key in ('integrity_sha256','verification_artifacts'):card.pop(key,None)
            if not card.get('article_title') or not card.get('summary'):raise IssueError('请填写篇名和有限范围的摘要')
            if card.get('source_id') and not conn.execute('SELECT 1 FROM sources WHERE source_id=?',(card['source_id'],)).fetchone():raise IssueError('来源ID不存在，可先提供公开来源链接')
            if len(json.dumps(card,ensure_ascii=False))>24000:raise IssueError('单卡内容过大，请拆分核验范围')
            identity=uuid.uuid4().hex
            conn.execute('INSERT INTO knowledge_drafts(id,base_revision,card_json,created_by) VALUES(?,?,?,?)',(identity,revision_in_transaction(conn),json.dumps(card,ensure_ascii=False),actor))
            conn.commit();return {'draft_id':identity,'status':'candidate'}
        finally:conn.close()

    def review(self, identity, *, actor, approved, quote_verified, scope, reason, source_checked):
        if type(approved)!=bool or type(quote_verified)!=bool or type(source_checked)!=bool:raise IssueError('核验选择必须明确')
        if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:raise IssueError('请填写核验说明')
        if not isinstance(scope,str) or not scope.strip() or len(scope)>1000:raise IssueError('请说明实际核验范围')
        conn=connect(self.db_path)
        try:
            conn.execute('BEGIN IMMEDIATE');row=self._draft(conn,identity)
            if row['status']=='published':raise IssueError('已发布草稿不能修改',409)
            card=json.loads(row['card_json'])
            if approved and (not source_checked or not card.get('source_urls')):raise IssueError('授予资格需要人工确认公开来源和核验范围')
            if quote_verified and (not approved or not card.get('excerpt') or not card.get('earliest_wording_date')):raise IssueError('逐字资格需要已核原文、措辞日期和摘要资格')
            card.update(reviewed=approved,quote_verified=quote_verified,review_scope=scope.strip(),quotation_scope=scope.strip() if quote_verified else '无逐字资格',editorial_review={'actor':actor,'reason':reason.strip()})
            card['integrity_sha256']=content_version(card)
            conn.execute("UPDATE knowledge_drafts SET status='reviewed',card_json=?,reviewed_by=?,review_reason=? WHERE id=?",(json.dumps(card,ensure_ascii=False),actor,reason.strip(),identity))
            conn.commit();return {'draft_id':identity,'status':'reviewed'}
        finally:conn.close()

    @staticmethod
    def _draft(conn, identity):
        if not isinstance(identity,str) or not re.fullmatch(r'[a-f0-9]{32}',identity):raise IssueError('草稿ID无效')
        row=conn.execute('SELECT * FROM knowledge_drafts WHERE id=?',(identity,)).fetchone()
        if row is None:raise IssueError('草稿不存在',404)
        return row

    def _preview(self, conn, row):
        if row['base_revision']!=revision_in_transaction(conn):raise IssueError('资料库已更新，保留了此草稿，请基于最新版本重新建立草稿',409)
        card=json.loads(row['card_json']);old=conn.execute('SELECT * FROM evidence_cards WHERE evidence_id=?',(card['id'],)).fetchone()
        duplicates=[r[0] for r in conn.execute('SELECT evidence_id FROM evidence_cards WHERE evidence_id!=? AND (article_title=? OR summary=?)',(card['id'],card['article_title'],card['summary']))]
        preview={'draft_id':row['id'],'status':row['status'],'base_revision':row['base_revision'],
                 'before':dict(old) if old else None,'after':card,'duplicates':duplicates,
                 'affected_interpretations':[p['principle_id'] for p in PRINCIPLES if card['id'] in p['evidence_ids']],
                 'affected_events':[r[0] for r in conn.execute('SELECT event_id FROM events WHERE evidence_id=?',(card['id'],))],
                 'notice':'修改卡片将同步停用关联思想解释，并撤回关联事件和关系的既有资格，需另行核验；新草稿不直接生效。'}
        preview['preview_token']=content_version(preview);return preview

    def preview(self,identity):
        conn=connect(self.db_path)
        try:
            conn.execute('BEGIN');return self._preview(conn,self._draft(conn,identity))
        finally:conn.close()

    def publish(self,identity,token,actor):
        conn=connect(self.db_path)
        try:
            conn.execute('BEGIN IMMEDIATE');row=self._draft(conn,identity)
            previous=conn.execute('SELECT id,after_revision FROM knowledge_releases WHERE draft_id=?',(identity,)).fetchone()
            if previous:return {'release_id':previous['id'],'revision':previous['after_revision'],'replayed':True}
            preview=self._preview(conn,row)
            if row['status']!='reviewed' or not isinstance(token,str) or token!=preview['preview_token']:raise IssueError('请先人工核验并重新预览当前变更',409)
            before=_snapshot(conn);card=json.loads(row['card_json'])
            _upsert_evidence(conn,card)
            # Derived evidence never retains a prior eligibility decision after editing its support.
            conn.execute("UPDATE events SET verification_status='candidate' WHERE evidence_id=?",(card['id'],))
            conn.execute("UPDATE quote_event_links SET verification_status='candidate' WHERE quote_id IN (SELECT quote_id FROM quotes WHERE evidence_id=?) OR event_id IN (SELECT event_id FROM events WHERE evidence_id=?)",(card['id'],card['id']))
            if not card.get('excerpt'):
                conn.execute('DELETE FROM quotes WHERE evidence_id=?',(card['id'],))
            else:conn.execute("UPDATE quotes SET verification_status=? WHERE evidence_id=?",('reviewed' if card['reviewed'] and card['quote_verified'] else 'candidate',card['id']))
            release=self._finish(conn,before,actor,draft_id=identity)
            conn.execute("UPDATE knowledge_drafts SET status='published' WHERE id=?",(identity,))
            conn.commit();return release
        except Exception:
            conn.rollback();raise
        finally:conn.close()

    def _finish(self,conn,before,actor,draft_id=None,rollback_of=None,before_revision=None):
        indexes=build_indexes(conn,own_transaction=False)
        # Retire stale hash vectors for edited curated material; FTS remains available.
        conn.execute("DELETE FROM embeddings WHERE target_type IN ('quote','event')")
        after=revision_in_transaction(conn)
        if conn.execute('PRAGMA foreign_key_check').fetchone():raise IssueError('资料关联校验失败，未发布',409)
        identity=uuid.uuid4().hex
        manifest={'schema':'curated-release-v1','indexes':indexes,'vector_status':'curated hash vectors invalidated; lexical available','reading_revision':after,'retrieval_revision':after}
        # Snapshot revision is taken from the stored before state, not the edited DB.
        prior=conn.execute('SELECT base_revision FROM knowledge_drafts WHERE id=?',(draft_id,)).fetchone() if draft_id else None
        before_revision=prior[0] if prior else before_revision
        conn.execute('INSERT INTO knowledge_releases(id,draft_id,before_revision,after_revision,before_json,manifest_json,actor,rollback_of) VALUES(?,?,?,?,?,?,?,?)',
                     (identity,draft_id,before_revision,after,json.dumps(before,ensure_ascii=False),json.dumps(manifest,ensure_ascii=False),actor,rollback_of))
        return {'release_id':identity,'revision':after,'manifest':manifest}

    def rollback(self,identity,expected_revision,actor):
        conn=connect(self.db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            latest=conn.execute('SELECT * FROM knowledge_releases ORDER BY rowid DESC LIMIT 1').fetchone()
            if not latest or latest['id']!=identity or revision_in_transaction(conn)!=expected_revision or latest['after_revision']!=expected_revision:raise IssueError('仅可回退当前最近发布版本，请刷新后核对',409)
            before=_snapshot(conn)
            _restore(conn,json.loads(latest['before_json']))
            result=self._finish(conn,before,actor,rollback_of=identity,before_revision=expected_revision)
            conn.commit();return result
        except Exception:
            conn.rollback();raise
        finally:conn.close()
