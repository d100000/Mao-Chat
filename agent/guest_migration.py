"""Recoverable guest resource claims without rerunning or rebilling answers.

Content capabilities are revoked at the points claim, before content moves.
An in-flight source stream may finish privately; resource guards prevent new
guest access and prevent account writes into a half-published migration.
"""
from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .issues import thread_id, public_metadata
from .points import PointsError, _identity, _operation


SCHEMA='''
CREATE TABLE IF NOT EXISTS guest_migration_jobs(
 id TEXT PRIMARY KEY, operation_key TEXT NOT NULL UNIQUE, digest TEXT NOT NULL,
 source_principal TEXT NOT NULL, target_principal TEXT NOT NULL,
 source_user INTEGER NOT NULL, target_user INTEGER NOT NULL,
 include_wallet INTEGER NOT NULL, include_content INTEGER NOT NULL,
 state TEXT NOT NULL, claim_result TEXT NOT NULL DEFAULT '{}',
 error_code TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_guest_migration_owner ON guest_migration_jobs(target_principal,state);
CREATE TABLE IF NOT EXISTS guest_migration_threads(
 source_user INTEGER NOT NULL, source_thread TEXT NOT NULL,
 target_user INTEGER NOT NULL, target_thread TEXT NOT NULL,
 job_id TEXT NOT NULL REFERENCES guest_migration_jobs(id),
 state TEXT NOT NULL, payload_hash TEXT,
 PRIMARY KEY(source_user,source_thread), UNIQUE(target_user,target_thread));
'''


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


class GuestMigrationStore:
    def __init__(self,points,issues,agent):
        self.points,self.issues,self.agent=points,issues,agent
        self.db_path=Path(issues.db_path)
        self.session_dir=Path(issues.session_dir)
        if Path(points.db_path).resolve()!=self.db_path.resolve():
            raise ValueError('migration and points stores must share their transaction database')
        with self._conn() as conn: conn.executescript(SCHEMA)

    def _conn(self):
        conn=sqlite3.connect(str(self.db_path),timeout=10)
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def claim(self,guest_actor,user_actor,operation_id,include_wallet=True,include_conversations=True):
        source,target=_identity(guest_actor),_identity(user_actor)
        operation_id=_operation(operation_id)
        if not source.startswith('g:') or not target.startswith('u:'):
            raise PointsError('归入身份无效',401)
        if type(include_wallet) is not bool or type(include_conversations) is not bool or not (include_wallet or include_conversations):
            raise PointsError('请选择需要归入的内容')
        if include_conversations and guest_actor.get('wallet_only'):
            raise PointsError('匿名钱包不能授权对话内容',403)
        digest=hashlib.sha256(_json([source,target,include_wallet,include_conversations]).encode()).hexdigest()
        key=target+':'+operation_id
        with self.issues.lock,self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous=conn.execute('SELECT * FROM guest_migration_jobs WHERE operation_key=?',(key,)).fetchone()
            if previous:
                if previous['digest']!=digest: raise PointsError('同一迁移操作不能更改参数',409,'idempotency_conflict')
                job_id=previous['id']
            else:
                guest=conn.execute('SELECT id,content_claimed_to FROM points_guests WHERE principal=?',(source,)).fetchone()
                if not guest: raise PointsError('访客空间不存在',404)
                if include_conversations and guest['content_claimed_to'] and guest['content_claimed_to']!=target:
                    raise PointsError('访客空间已归入其他账户',409,'claim_conflict')
                job_id=uuid.uuid4().hex
                conn.execute('INSERT INTO guest_migration_jobs(id,operation_key,digest,source_principal,target_principal,source_user,target_user,include_wallet,include_content,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                             (job_id,key,digest,source,target,-guest['id'],int(target[2:]),int(include_wallet),int(include_conversations),'prepared',time.time(),time.time()))
        return self.process(job_id,user_actor)

    def resume(self,operation_id,user_actor,include_wallet=True,include_conversations=True):
        """Retry a claimed task using its account-bound immutable operation ID."""
        target=_identity(user_actor); operation_id=_operation(operation_id)
        if not target.startswith('u:'): raise PointsError('请先登录账户',401)
        with self._conn() as conn:
            job=conn.execute('SELECT * FROM guest_migration_jobs WHERE operation_key=? AND target_principal=?',(target+':'+operation_id,target)).fetchone()
        if not job: raise PointsError('迁移任务不存在',404)
        source={'principal':job['source_principal'],'kind':'guest','storage_id':job['source_user'],'user_id':job['source_user']}
        return self.claim(source,user_actor,operation_id,include_wallet=include_wallet,include_conversations=include_conversations)

    def _claim_resources(self,job):
        source={'principal':job['source_principal'],'kind':'guest','storage_id':job['source_user'],'user_id':job['source_user']}
        target={'principal':job['target_principal'],'kind':'user','storage_id':job['target_user'],'user_id':job['target_user']}
        operation='migration-'+job['id']
        try:
            if job['include_wallet'] and job['include_content']:
                result=self.points.claim_both(source,target,operation)
            elif job['include_wallet']:
                result=self.points.claim_guest(source,target,operation)
            else:
                result=self.points.claim_content(source,target,operation)
        except PointsError as exc:
            if exc.status in {401,403,404,409}:
                with self._conn() as conn:
                    conn.execute("UPDATE guest_migration_jobs SET state='aborted',error_code='claim_conflict',updated=? WHERE id=?",(time.time(),job['id']))
            raise
        with self._conn() as conn:
            conn.execute('UPDATE guest_migration_jobs SET state=?,claim_result=?,updated=? WHERE id=?',('pending' if job['include_content'] else 'committed',_json(result),time.time(),job['id']))

    def _job(self,conn,job_id,user_actor=None):
        if not isinstance(job_id,str) or not re.fullmatch(r'[a-f0-9]{32}',job_id): raise PointsError('迁移标识无效',404)
        job=conn.execute('SELECT * FROM guest_migration_jobs WHERE id=?',(job_id,)).fetchone()
        if not job or (user_actor is not None and _identity(user_actor)!=job['target_principal']):
            raise PointsError('迁移任务不存在',404)
        return job

    def assert_available(self,storage_id,tid=None):
        """HTTP authorization guard; do not call on already registered streams."""
        if type(storage_id) is not int: raise PointsError('内容身份无效',401)
        if storage_id<0 and self.points.content_owner(storage_id)!=storage_id:
            raise PointsError('访客记录已归入账户',401,'content_claimed')
        if tid is None: return True  # Lists may show completed rows, with job status separately.
        tid=thread_id(tid)
        with self._conn() as conn:
            row=conn.execute("SELECT 1 FROM guest_migration_threads m JOIN guest_migration_jobs j ON j.id=m.job_id WHERE m.target_user=? AND m.target_thread=? AND m.state<>'skipped' AND (m.state<>'committed' OR j.state<>'committed')",(storage_id,tid)).fetchone()
            if row: raise PointsError('当前议题正在归入，请稍后重试',409,'migration_pending')
        return True

    def _active(self,conn,job):
        owners={job['source_user'],job['target_user']}
        if any(key[0] in owners for key in self.issues.active_streams): return True
        return bool(conn.execute("SELECT 1 FROM points_requests WHERE principal IN (?,?) AND state IN ('reserved','running') LIMIT 1",(job['source_principal'],job['target_principal'])).fetchone())

    def _path(self,owner,tid):
        return self.issues._path(owner,thread_id(tid))

    def _stage(self,mapping):
        key=hashlib.sha256((str(mapping['source_user'])+':'+mapping['source_thread']).encode()).hexdigest()[:32]
        return self.session_dir/('.migration-'+key+'.json')

    @staticmethod
    def _read(path):
        try:
            fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        except FileNotFoundError:
            return None
        with os.fdopen(fd,'r',encoding='utf-8') as handle:
            raw=handle.read(8*1024*1024+1)
        if len(raw)>8*1024*1024: raise ValueError('migration payload too large')
        payload=json.loads(raw)
        if not isinstance(payload,dict) or not isinstance(payload.get('messages',[]),list) or not isinstance(payload.get('metadata',{}),dict):
            raise ValueError('invalid session snapshot')
        return payload

    def _atomic_file(self,path,payload):
        self.session_dir.mkdir(parents=True,exist_ok=True)
        os.chmod(self.session_dir,0o700)
        temporary=self.session_dir/('.write-'+uuid.uuid4().hex+'.json')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as handle:
                handle.write(_json(payload)+'\n'); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary,path); os.chmod(path,0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def _target_exists(self,conn,owner,tid):
        return (conn.execute('SELECT 1 FROM issue_state WHERE user_id=? AND thread_id=?',(owner,tid)).fetchone()
                or conn.execute('SELECT 1 FROM sessions WHERE session_id=?',(self.issues.session_id(owner,tid),)).fetchone()
                or conn.execute('SELECT 1 FROM guest_migration_threads WHERE target_user=? AND target_thread=?',(owner,tid)).fetchone()
                or self._path(owner,tid).exists() or (owner,tid) in self.issues.active_streams)

    def _mapping(self,conn,job,tid):
        mapping=conn.execute('SELECT * FROM guest_migration_threads WHERE source_user=? AND source_thread=?',(job['source_user'],tid)).fetchone()
        if mapping:
            if mapping['target_user']!=job['target_user']: raise PointsError('记录已归入其他账户',409,'claim_conflict')
            return mapping
        candidate=tid
        for attempt in range(100):
            if not self._target_exists(conn,job['target_user'],candidate): break
            candidate='guest-'+hashlib.sha256((job['source_principal']+':'+tid+':'+str(attempt)).encode()).hexdigest()[:24]
        else: raise PointsError('议题归入暂不可用',503)
        conn.execute('INSERT INTO guest_migration_threads VALUES(?,?,?,?,?,?,?)',(job['source_user'],tid,job['target_user'],candidate,job['id'],'prepared',None))
        return conn.execute('SELECT * FROM guest_migration_threads WHERE source_user=? AND source_thread=?',(job['source_user'],tid)).fetchone()

    def _source_threads(self,conn,job,limit):
        source=job['source_user']; found=[]
        def add(tid):
            if tid in found: return
            try: thread_id(tid)
            except ValueError: return
            row=conn.execute('SELECT deleted FROM issue_state WHERE user_id=? AND thread_id=?',(source,tid)).fetchone()
            if row and row['deleted']: return
            mapping=conn.execute('SELECT state FROM guest_migration_threads WHERE source_user=? AND source_thread=?',(source,tid)).fetchone()
            if not mapping or mapping['state'] not in {'committed','skipped'}: found.append(tid)
        for row in conn.execute('SELECT thread_id FROM issue_state WHERE user_id=? AND deleted=0 ORDER BY thread_id LIMIT ?',(source,limit)):
            add(row['thread_id'])
        prefix=self.issues.session_id(source,'placeholder').removesuffix('placeholder')
        pattern=prefix.replace('\\','\\\\').replace('_','\\_').replace('%','\\%')+'%'
        for row in conn.execute("SELECT session_id FROM sessions WHERE session_id LIKE ? ESCAPE '\\' ORDER BY session_id LIMIT ?",(pattern,limit)):
            add(row['session_id'][len(prefix):])
        # scandir is incremental rather than materializing every file in memory.
        if len(found)<limit and self.session_dir.exists():
            with os.scandir(self.session_dir) as entries:
                for entry in entries:
                    if entry.name.startswith(prefix) and entry.name.endswith('.json'):
                        add(entry.name[len(prefix):-5])
                        if len(found)>=limit: break
        return found[:limit]

    def _purge_deleted(self,job,limit):
        """A stale snapshot cannot revive a previously deleted guest topic."""
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows=conn.execute('SELECT thread_id FROM issue_state WHERE user_id=? AND deleted=1 LIMIT ?',(job['source_user'],limit)).fetchall()
            for row in rows:
                sid=self.issues.session_id(job['source_user'],row['thread_id'])
                mapping=conn.execute('SELECT * FROM guest_migration_threads WHERE source_user=? AND source_thread=?',(job['source_user'],row['thread_id'])).fetchone()
                if mapping and mapping['state']!='committed':
                    target_sid=self.issues.session_id(mapping['target_user'],mapping['target_thread'])
                    conn.execute('DELETE FROM llm_runs WHERE session_id=?',(target_sid,))
                    conn.execute('DELETE FROM sessions WHERE session_id=?',(target_sid,))
                    conn.execute('DELETE FROM issue_state WHERE user_id=? AND thread_id=?',(mapping['target_user'],mapping['target_thread']))
                    for table in ('issue_feedback','quality_runs'):
                        if self._table(conn,table): conn.execute('DELETE FROM '+table+' WHERE user_id=? AND thread_id=?',(mapping['target_user'],mapping['target_thread']))
                    self._path(mapping['target_user'],mapping['target_thread']).unlink(missing_ok=True)
                    self._stage(mapping).unlink(missing_ok=True)
                    conn.execute("UPDATE guest_migration_threads SET state='skipped' WHERE source_user=? AND source_thread=?",(job['source_user'],row['thread_id']))
                conn.execute('DELETE FROM llm_runs WHERE session_id=?',(sid,))
                conn.execute('DELETE FROM sessions WHERE session_id=?',(sid,))
                for table in ('issue_feedback','quality_runs'):
                    if self._table(conn,table): conn.execute('DELETE FROM '+table+' WHERE user_id=? AND thread_id=?',(job['source_user'],row['thread_id']))
                self._path(job['source_user'],row['thread_id']).unlink(missing_ok=True)
                conn.execute('DELETE FROM issue_state WHERE user_id=? AND thread_id=?',(job['source_user'],row['thread_id']))

    @staticmethod
    def _table(conn,name):
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())

    def _payload(self,conn,job,mapping):
        source_sid=self.issues.session_id(job['source_user'],mapping['source_thread'])
        payload=self._read(self._path(job['source_user'],mapping['source_thread']))
        session=conn.execute('SELECT * FROM sessions WHERE session_id=?',(source_sid,)).fetchone()
        if payload is None:
            payload={'messages':[],'metadata':json.loads(session['metadata_json']) if session else {}}
            for message in conn.execute('SELECT * FROM messages WHERE session_id=? ORDER BY message_id',(source_sid,)):
                payload['messages'].append({'role':message['role'],'content':message['content'],'evidence_ids':json.loads(message['evidence_ids_json'])})
        row=conn.execute('SELECT * FROM issue_state WHERE user_id=? AND thread_id=?',(job['source_user'],mapping['source_thread'])).fetchone()
        if row and row['deleted']: raise PointsError('已删除的议题不能归入',409)
        meta={**payload.get('metadata',{}),**(json.loads(row['state_json']) if row else {}),'user_id':job['target_user']}
        memories=[]
        for memory in meta.get('used_memories',[])[:3] if isinstance(meta.get('used_memories'),list) else []:
            if not isinstance(memory,dict): continue
            source_tid=memory.get('thread_id')
            try: thread_id(source_tid)
            except ValueError: continue
            exists=conn.execute('SELECT deleted FROM issue_state WHERE user_id=? AND thread_id=?',(job['source_user'],source_tid)).fetchone()
            if exists and not exists['deleted']:
                target=self._mapping(conn,job,source_tid)
                memories.append({**memory,'thread_id':target['target_thread']})
        meta['used_memories']=memories
        return {'session_id':self.issues.session_id(job['target_user'],mapping['target_thread']),
                'messages':payload.get('messages',[]),'metadata':meta}

    def _move_database(self,conn,job,mapping,payload):
        source,target=job['source_user'],job['target_user']; old_tid,new_tid=mapping['source_thread'],mapping['target_thread']
        old_sid,new_sid=self.issues.session_id(source,old_tid),self.issues.session_id(target,new_tid)
        old_session=conn.execute('SELECT * FROM sessions WHERE session_id=?',(old_sid,)).fetchone()
        meta=payload['metadata']; public=public_metadata(meta)
        if not public.get('title'):
            public['title']=next((str(x.get('content',''))[:60] for x in payload['messages'] if isinstance(x,dict) and x.get('role')=='user'),'未命名议题')
        if old_session:
            conn.execute('INSERT INTO sessions(session_id,mode,as_of,created_at,updated_at,metadata_json) VALUES(?,?,?,?,?,?)',
                         (new_sid,old_session['mode'],old_session['as_of'],old_session['created_at'],old_session['updated_at'],_json(meta)))
        else:
            conn.execute('INSERT INTO sessions(session_id,mode,as_of,metadata_json) VALUES(?,?,?,?)',(new_sid,meta.get('mode','consult'),meta.get('as_of'),_json(meta)))
        old_messages=defaultdict(deque)
        for old in conn.execute('SELECT * FROM messages WHERE session_id=? ORDER BY message_id',(old_sid,)):
            old_messages[(old['role'],old['content'])].append(old)
        retained=[]
        for message in payload['messages']:
            if not isinstance(message,dict) or message.get('role') not in {'user','assistant','system','tool'}: continue
            role,content=message['role'],str(message.get('content',''))
            old=old_messages[(role,content)].popleft() if old_messages[(role,content)] else None
            if old:
                conn.execute('UPDATE messages SET session_id=? WHERE message_id=?',(new_sid,old['message_id']))
                retained.append(old['message_id'])
            else:
                conn.execute('INSERT INTO messages(session_id,role,content,evidence_ids_json,metadata_json) VALUES(?,?,?,?,?)',
                             (new_sid,role,content,_json(message.get('evidence_ids',[])),_json({'source':'guest_migration'})))
        # Remaining old/pruned messages cascade only their own stale citations.
        conn.execute('DELETE FROM messages WHERE session_id=?',(old_sid,))
        conn.execute('UPDATE llm_runs SET session_id=? WHERE session_id=?',(new_sid,old_sid))
        conn.execute('DELETE FROM sessions WHERE session_id=?',(old_sid,))
        old_state=conn.execute('SELECT * FROM issue_state WHERE user_id=? AND thread_id=?',(source,old_tid)).fetchone()
        conn.execute('INSERT INTO issue_state(user_id,thread_id,title,archived,remember,deleted,revision,updated_at,state_json) VALUES(?,?,?,?,?,?,?,?,?)',
                     (target,new_tid,public['title'],int(public.get('archived',False)),int(public.get('remember',False)),0,
                      int(old_state['revision']) if old_state else 0,old_state['updated_at'] if old_state else self._stamp(),_json(public)))
        conn.execute('DELETE FROM issue_state WHERE user_id=? AND thread_id=?',(source,old_tid))
        for table in ('issue_feedback','quality_runs'):
            if self._table(conn,table):
                conn.execute('UPDATE '+table+' SET user_id=?,thread_id=? WHERE user_id=? AND thread_id=?',(target,new_tid,source,old_tid))

    @staticmethod
    def _stamp():
        from datetime import datetime,timezone
        return datetime.now(timezone.utc).isoformat()

    def _publish(self,mapping):
        stage=self._stage(mapping)
        target=self._path(mapping['target_user'],mapping['target_thread'])
        expected=mapping['payload_hash']
        if stage.exists():
            raw=stage.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=expected: raise ValueError('migration stage changed')
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()!=expected:
                raise ValueError('migration target already exists')
            os.replace(stage,target); os.chmod(target,0o600)
        elif not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest()!=expected:
            raise ValueError('migration snapshot unavailable')
        self._path(mapping['source_user'],mapping['source_thread']).unlink(missing_ok=True)
        with self._conn() as conn:
            conn.execute("UPDATE guest_migration_threads SET state='committed' WHERE source_user=? AND source_thread=?",(mapping['source_user'],mapping['source_thread']))

    def _move_one(self,job,tid):
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # Recheck under the same write lock that excludes a new points
            # reservation; the preliminary worker check alone has a race.
            if self._active(conn,job): return False
            mapping=self._mapping(conn,job,tid)
            if mapping['state'] in {'committed','skipped'}: return True
            if mapping['state']=='prepared':
                payload=self._payload(conn,job,mapping)
                self._atomic_file(self._stage(mapping),payload)
                expected=hashlib.sha256(self._stage(mapping).read_bytes()).hexdigest()
                self._move_database(conn,job,mapping,payload)
                conn.execute("UPDATE guest_migration_threads SET state='db_committed',payload_hash=? WHERE source_user=? AND source_thread=?",(expected,job['source_user'],tid))
            mapping=dict(conn.execute('SELECT * FROM guest_migration_threads WHERE source_user=? AND source_thread=?',(job['source_user'],tid)).fetchone())
        self._publish(mapping)
        return True

    def process(self,job_id,user_actor=None,batch=50):
        if type(batch) is not int or not 1<=batch<=100: raise PointsError('迁移批次无效')
        with self.issues.lock:
            with self._conn() as conn: job=self._job(conn,job_id,user_actor)
            if job['state'] in {'committed','aborted'}: return self.status(job_id,user_actor,process=False)
            if job['state']=='prepared':
                self._claim_resources(job)
                with self._conn() as conn: job=self._job(conn,job_id,user_actor)
            if not job['include_content']: return self.status(job_id,user_actor,process=False)
            with self._conn() as conn:
                if self._active(conn,job): return self.status(job_id,user_actor,process=False)
                recovery=[row['source_thread'] for row in conn.execute("SELECT source_thread FROM guest_migration_threads WHERE source_user=? AND target_user=? AND state NOT IN ('committed','skipped') ORDER BY source_thread LIMIT ?",(job['source_user'],job['target_user'],batch))]
                candidates=self._source_threads(conn,job,batch)
            work=list(dict.fromkeys(recovery+candidates))[:batch]
            try:
                self._purge_deleted(job,batch)
                for tid in work:
                    if not self._move_one(job,tid): break
                with self._conn() as conn:
                    remaining=self._source_threads(conn,job,1)
                    unfinished=conn.execute("SELECT 1 FROM guest_migration_threads WHERE source_user=? AND target_user=? AND state NOT IN ('committed','skipped') LIMIT 1",(job['source_user'],job['target_user'])).fetchone()
                    deleted=conn.execute('SELECT 1 FROM issue_state WHERE user_id=? AND deleted=1 LIMIT 1',(job['source_user'],)).fetchone()
                    conn.execute('UPDATE guest_migration_jobs SET state=?,error_code=NULL,updated=? WHERE id=?',('pending' if remaining or unfinished or deleted else 'committed',time.time(),job_id))
            except (OSError,ValueError,sqlite3.Error):
                with self._conn() as conn:
                    conn.execute("UPDATE guest_migration_jobs SET state='retryable',error_code='migration_io',updated=? WHERE id=?",(time.time(),job_id))
            return self.status(job_id,user_actor,process=False)

    def status(self,job_id,user_actor=None,process=True,offset=0,limit=1000):
        if type(offset) is not int or not 0<=offset<=1000000 or type(limit) is not int or not 1<=limit<=1000:
            raise PointsError('迁移分页参数无效')
        if process:
            self.process(job_id,user_actor)
            return self.status(job_id,user_actor,process=False,offset=offset,limit=limit)
        with self._conn() as conn:
            job=self._job(conn,job_id,user_actor)
            mappings=conn.execute("SELECT source_thread,target_thread,state FROM guest_migration_threads WHERE source_user=? AND target_user=? AND state<>'skipped' ORDER BY source_thread LIMIT ? OFFSET ?",(job['source_user'],job['target_user'],limit+1,offset)).fetchall()
            migrated=conn.execute("SELECT COUNT(*) FROM guest_migration_threads WHERE source_user=? AND target_user=? AND state='committed'",(job['source_user'],job['target_user'])).fetchone()[0]
        has_more=len(mappings)>limit; mappings=mappings[:limit]
        return {'ok':True,'job_id':job['id'],'migration_id':job['id'],'state':job['state'],
                'source_storage_id':job['source_user'],'target_storage_id':job['target_user'],
                **json.loads(job['claim_result']),
                'thread_map':{row['source_thread']:row['target_thread'] for row in mappings},
                'migrated':migrated,'has_more':has_more,'next_offset':offset+limit if has_more else None,
                'retryable':job['state']=='retryable',
                'error': '记录归入尚未完成，请稍后重试' if job['error_code'] else None}

    def resume_pending(self,limit=10,batch=50):
        if type(limit) is not int or not 1<=limit<=100: raise PointsError('迁移批次无效')
        with self._conn() as conn:
            jobs=[row['id'] for row in conn.execute("SELECT id FROM guest_migration_jobs WHERE state IN ('prepared','pending','retryable') ORDER BY updated LIMIT ?",(limit,))]
        output=[]
        for job in jobs:
            try: output.append(self.process(job,batch=batch))
            except PointsError: continue
        return output

    def cleanup_stale(self,storage_id):
        """Purge expired guest content only, preserving independent money data."""
        if type(storage_id) is not int or storage_id>=0: raise PointsError('仅清理过期访客内容')
        with self.issues.lock,self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            guest=conn.execute('SELECT * FROM points_guests WHERE id=?',(-storage_id,)).fetchone()
            if not guest or guest['last_active']>self.points._now()-self.points.trial_seconds: return False
            if any(key[0]==storage_id for key in self.issues.active_streams): return False
            if conn.execute("SELECT 1 FROM points_requests WHERE principal=? AND state IN ('reserved','running') LIMIT 1",(guest['principal'],)).fetchone(): return False
            if conn.execute("SELECT 1 FROM guest_migration_jobs WHERE source_user=? AND include_content=1 AND state NOT IN ('committed','aborted') LIMIT 1",(storage_id,)).fetchone(): return False
            prefix=self.issues.session_id(storage_id,'placeholder').removesuffix('placeholder')
            pattern=prefix.replace('\\','\\\\').replace('_','\\_').replace('%','\\%')+'%'
            conn.execute("DELETE FROM llm_runs WHERE session_id LIKE ? ESCAPE '\\'",(pattern,))
            conn.execute("DELETE FROM sessions WHERE session_id LIKE ? ESCAPE '\\'",(pattern,))
            conn.execute('DELETE FROM issue_state WHERE user_id=?',(storage_id,))
            for table in ('issue_feedback','quality_runs'):
                if self._table(conn,table): conn.execute('DELETE FROM '+table+' WHERE user_id=?',(storage_id,))
            if self.session_dir.exists():
                with os.scandir(self.session_dir) as entries:
                    for entry in entries:
                        if not(entry.name.startswith(prefix) and entry.name.endswith('.json')): continue
                        try: tid=thread_id(entry.name[len(prefix):-5])
                        except ValueError: continue
                        self._path(storage_id,tid).unlink(missing_ok=True)
            return True
