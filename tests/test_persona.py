import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
SCRIPTS=ROOT/'skills/mao-primary-sources/scripts'
sys.path.insert(0,str(SCRIPTS))
import persona as p
import retrieve as r

class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.c=dict(id='X',reviewed=True,speaker='毛泽东',source_role='author_body',content_time_verified=True,earliest_content_date='1937-08-31',earliest_wording_date='1950-12-29',known_to_character_from='1937-08-31',quote_verified=True)
    def test_content_and_wording_are_independent(self):
        self.assertTrue(p.available(self.c,p.valid_day('1938-06-30'),'paraphrase'))
        self.assertFalse(p.available(self.c,p.valid_day('1938-06-30'),'quote'))
        self.assertTrue(p.available(self.c,p.valid_day('1951-01-01'),'quote'))
    def test_unknown_or_future_dates_block(self):
        for key in ('earliest_content_date','known_to_character_from'):
            for value in (None,'bad','2000-01-01'):
                self.assertFalse(p.available({**self.c,key:value},p.valid_day('1938-06-30')))
    def test_strings_cannot_enable_review(self):
        for key in ('reviewed','content_time_verified'):
            self.assertFalse(p.available({**self.c,key:'true'},p.valid_day('1938-06-30')))
        self.assertFalse(p.available({**self.c,'quote_verified':'true'},p.valid_day('1951-01-01'),'quote'))
    def test_other_speaker_and_editor_block(self):
        self.assertFalse(p.available({**self.c,'speaker':'林彪'},p.valid_day('1938-06-30')))
        self.assertFalse(p.available({**self.c,'source_role':'editor_note'},p.valid_day('1938-06-30')))
    def test_biography_requires_direct_public_event_and_no_quote(self):
        c={**self.c,'source_role':'biographical_record','speaker':'机构','actor':'毛泽东','public_event_verified':True}
        self.assertTrue(p.available(c,p.valid_day('1938-06-30')))
        self.assertFalse(p.available(c,p.valid_day('1951-01-01'),'quote'))
        self.assertFalse(p.available({**c,'public_event_verified':False},p.valid_day('1938-06-30')))

class RealDataTests(unittest.TestCase):
    def setUp(self):self.cards={c['id']:c for c in p.load_cards()}
    def test_record_integrity_and_source_fingerprints(self):self.assertTrue(p.audit()['passed'])
    def test_modern_mode_handles_research_only_timeline(self):
        packet=p.prepare('AI产品 调查研究 工作方法','modern',None)
        self.assertGreater(len(packet['evidence']),0)
        self.assertIsInstance(self.cards['TL10']['summary'],str)
        self.assertGreater(len(self.cards['TL10']['summary']),0)
        self.assertEqual(p.score({'topic':None,'article_title':None,'summary':None,'excerpt':None,'topics':None},'研究'),0)
    def test_march_1957_cannot_use_june_published_text(self):
        for eid in ('VL08','VL09','VL10'):
            self.assertFalse(p.available(self.cards[eid],p.valid_day('1957-03-01')))
            self.assertTrue(p.available(self.cards[eid],p.valid_day('1957-06-19')))
    def test_no_false_may_complete_speech(self):
        self.assertFalse(p.available(self.cards['VE06'],p.valid_day('1938-05-31')))
        self.assertTrue(p.available(self.cards['VE06'],p.valid_day('1938-06-03')))
    def test_prefilter_applies_to_ranking(self):
        packet=p.prepare('1957人民内部矛盾 六条标准','historical','1938-06-30',limit=12)
        ids={c['id'] for c in packet['evidence']}
        self.assertFalse(ids & {'VL08','VL09','VL10','VL11','VL12'})
        self.assertEqual(packet['future_years_in_question'],[1957])
    def test_unqualified_words_are_hidden_from_historical_packet(self):
        packet=p.prepare('实践认识理论','historical','1938-06-30',limit=12)
        card=next(c for c in packet['evidence'] if c['id']=='VE04')
        self.assertFalse(card['quote_eligible_at_role_date']);self.assertEqual(card['excerpt'],'')
    def test_exact_quote_and_date_validation(self):
        c=self.cards['VL03'];txt=c['excerpt'];draft={'answer':txt+'【VL03】','claims':[{'text':txt,'kind':'quote','evidence_ids':['VL03']}]}
        self.assertTrue(p.check_draft(draft,'historical','1949-07-01')['passed'])
        self.assertFalse(p.check_draft(draft,'historical','1949-06-30')['passed'])
        bad={'answer':txt+'永久不变。【VL03】','claims':[{'text':txt+'永久不变。','kind':'quote','evidence_ids':['VL03']}]}
        self.assertFalse(p.check_draft(bad,'research',None)['passed'])
    def test_unknown_source_and_outside_packet_rejected(self):
        draft={'answer':'某个主张。【FAKE】','claims':[{'text':'某个主张。','kind':'paraphrase','evidence_ids':['FAKE']}]}
        self.assertFalse(p.check_draft(draft,'research',None)['passed'])
        c=self.cards['VE01'];draft={'answer':c['summary']+'【VE01】','claims':[{'text':c['summary'],'kind':'paraphrase','evidence_ids':['VE01']}]}
        self.assertFalse(p.check_draft(draft,'historical','1927-03-31',[])['passed'])
    def test_1957_later_political_material_is_available(self):
        pool,_=p.retrieve('反右 路线','historical','1957-07-02',12)
        self.assertTrue({'VL11','VL12'}.issubset({c['id'] for c in pool}))
    def test_research_only_timeline_does_not_become_memory(self):
        self.assertFalse(p.available(self.cards['TL10'],p.valid_day('1957-07-01')))
    def test_malformed_draft_and_quote_requirements(self):
        self.assertFalse(p.check_draft({'answer':'x'},'research',None)['passed'])
        self.assertFalse(p.check_draft({'answer':'x【VL03】','claims':[]},'research',None)['passed'])
    def test_record_keeps_cutoff_and_does_not_change_evidence(self):
        before=p.DATA.read_bytes()
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'session.json';p.create_session(path,'historical','1927-03-31')
            with self.assertRaises(ValueError):p.prepare('q','historical','1957-07-01',path)
            packet=p.prepare('湖南农民运动',None,None,path)
            c=self.cards['VE01'];draft={'answer':c['summary']+'【VE01】','claims':[{'text':c['summary'],'kind':'paraphrase','evidence_ids':['VE01']}]}
            saved=p.record(path,draft,packet);self.assertTrue(saved['saved'])
            nxt=p.prepare('继续',None,None,path);self.assertEqual(nxt['as_of'],'1927-03-31');self.assertEqual(len(nxt['conversation']),1)
            old=path.read_bytes();draft['claims'][0]['evidence_ids']=['VL12'];draft['answer']='后来。【VL12】';draft['claims'][0]['text']='后来。'
            self.assertFalse(p.record(path,draft,packet)['saved']);self.assertEqual(old,path.read_bytes())
        self.assertEqual(before,p.DATA.read_bytes())
    def test_search_query_does_not_rewrite_user_message(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'session.json';p.create_session(path,'historical','1945-04-25')
            original='为什么此时允许有益私人资本？';query='论联合政府 私人资本 阶段纲领'
            packet=p.prepare(original,None,None,path,6,query)
            self.assertEqual(packet['question'],original);self.assertEqual(packet['retrieval_query'],query)
            c=self.cards['VE12'];draft={'answer':c['summary']+'【VE12】','claims':[{'text':c['summary'],'kind':'paraphrase','evidence_ids':['VE12']}]}
            self.assertTrue(p.record(path,draft,packet)['saved'])
            turn=json.loads(path.read_text())['turns'][0]
            self.assertEqual(turn['user'],original);self.assertEqual(turn['retrieval_query'],query)
    def test_url_year_does_not_trigger_future_knowledge_warning(self):
        draft={'answer':'无法承诺未来结果。参考 https://example.org/2025/source','claims':[]}
        checked=p.check_draft(draft,'historical','1938-06-30')
        self.assertFalse(any('2025' in x for x in checked['warnings']))
        checked=p.check_draft({'answer':'不能回忆2025年的事情。','claims':[]},'historical','1938-06-30')
        self.assertTrue(any('2025' in x for x in checked['warnings']))
    def test_failed_and_successful_attempts_are_both_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'session.json';p.create_session(path,'historical','1927-03-31')
            packet=p.prepare('湖南调查',None,None,path)
            bad={'answer':'未知主张。【BAD】','claims':[{'text':'未知主张。','kind':'paraphrase','evidence_ids':['BAD']}]}
            self.assertFalse(p.record(path,bad,packet)['saved'])
            c=self.cards['VE01'];good={'answer':c['summary']+'【VE01】','claims':[{'text':c['summary'],'kind':'paraphrase','evidence_ids':['VE01']}]}
            self.assertTrue(p.record(path,good,packet)['saved'])
            logs=[json.loads(x.read_text()) for x in (Path(td)/'attempts').glob('*.json')]
            self.assertEqual(len(logs),2)
            self.assertEqual({x['check']['passed'] for x in logs},{True,False})
            self.assertTrue(any(x['draft']==bad for x in logs))
            self.assertEqual(len(json.loads(path.read_text())['turns']),1)
    def test_tampered_evidence_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'cards.jsonl';c=dict(self.cards['VE01']);c['earliest_content_date']='1900-01-01';path.write_text(json.dumps(c))
            with patch.object(p,'DATA',path):
                with self.assertRaises(ValueError):p.load_cards()

class IndexIntegrityTests(unittest.TestCase):
    def test_failed_rebuild_preserves_old_index(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'corpus').mkdir();db=root/'corpus/search.sqlite'
            conn=sqlite3.connect(db);conn.execute('create table marker(value text)');conn.execute("insert into marker values('old')");conn.commit();conn.close()
            (root/'corpus/01_bad.pages.jsonl').write_text('{broken json')
            with self.assertRaises(json.JSONDecodeError):r.build(root)
            conn=sqlite3.connect(db);self.assertEqual(conn.execute('select value from marker').fetchone()[0],'old');conn.close()
            self.assertEqual(list((root/'corpus').glob('search-building-*')),[])
    @unittest.skipUnless((ROOT/'corpus/search.sqlite').is_file(), '可选全文索引未安装；已核资料测试仍执行。')
    def test_existing_full_corpus_and_context(self):
        conn=sqlite3.connect(ROOT/'corpus/search.sqlite');self.assertEqual(conn.execute('select count(distinct anchor) from pages').fetchone()[0],2239);conn.close()
        out=r.get_page(ROOT,'01_selected_works_v1:p0270',1)
        self.assertEqual([x['pdf_page'] for x in out['pages']],[269,270,271])

if __name__=='__main__':unittest.main()
