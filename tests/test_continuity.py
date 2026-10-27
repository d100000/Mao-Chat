import json
import unittest
import uuid
from agent.auth_server import _bounded_history, _issue_prompt_history
from agent.continuity import resume_summary, RECENT_CONTEXT_CHARS
from agent.statements import add_turn, update_statement
from tests.test_statements import understanding


class ContinuityTests(unittest.TestCase):
    def metadata(self):
        meta={'mode':'consult'}
        add_turn(meta,understanding(statement_sources=[{'field':'stage_goal','quote':'保住客户'},{'field':'known_facts:0','quote':'团队两人'}]),'希望保住客户，团队两人，没有额外预算。',uuid.uuid4().hex,'t1')
        return meta

    def test_resume_tracks_correction_pause_and_actual_action_state(self):
        meta=self.metadata();original=resume_summary(meta)
        self.assertEqual(original['goal']['source_excerpt'],'保住客户')
        record=next(x for x in meta['statements'] if x['field']=='stage_goal')
        update_statement(meta,record['statement_id'],corrected_value='先暂停接新客户',timestamp='t2')
        updated=resume_summary(meta)
        self.assertEqual(updated['goal']['value'],'先暂停接新客户')
        self.assertNotEqual(updated['version'],original['version'])
        meta['actions']=[{'id':'a','title':'助手提议','status':'proposed'},{'id':'b','title':'核对一次','status':'accepted','feedback':''}]
        self.assertEqual([x['id'] for x in resume_summary(meta)['actions']],['b'])
        self.assertEqual(resume_summary(meta)['actions'][0]['status'],'accepted')
        active=next(x for x in meta['statements'] if x['field']=='stage_goal' and x['status']=='active')
        update_statement(meta,active['statement_id'],'inactive',timestamp='t3')
        self.assertIsNone(resume_summary(meta)['goal'])
        self.assertIsNone(resume_summary({}))
        self.assertIsNone(resume_summary({**meta,'mode':'historical'}))

    def test_long_context_reduces_old_text_preserves_controls_and_latest_user(self):
        meta=self.metadata();constraint=next(x for x in meta['statements'] if x['field']=='constraints:0')
        update_statement(meta,constraint['statement_id'],'inactive',timestamp='t2')
        history=[{'role':'user' if i%2==0 else 'assistant','content':str(i)+'旧背景'*2000} for i in range(60)]
        history.append({'role':'user','content':'最新纠正：不再沿用旧预算，先核对实际开支。'})
        before=_bounded_history(history)
        after=_issue_prompt_history(history,'consult',meta,[])
        old=sum(len(x['content']) for x in before);new=sum(len(x['content']) for x in after)
        self.assertLess(new,old*.5)
        self.assertIn(history[-1],after)
        note=next(x for x in after if '本议题可纠正的信息' in x['content'])
        controls=json.loads(note['content'].split('\n',1)[1])
        self.assertTrue(any(r['value']=='没有额外预算' for r in controls['excluded']))
        self.assertTrue(all('source_turn_id' in r for r in controls['excluded']))
        self.assertLessEqual(sum(len(x['content']) for x in after if x['role']=='user'),RECENT_CONTEXT_CHARS)

    def test_latest_user_wins_over_oversize_assistant_and_old_opening(self):
        history=[{'role':'user','content':'旧'*100},{'role':'assistant','content':'大'*200},{'role':'user','content':'最新原话'}]
        self.assertIn(history[-1],_bounded_history(history,max_chars=6))
        self.assertNotIn(history[0],_bounded_history(history,max_chars=6))

    def test_reading_does_not_evict_excluded_controls(self):
        meta=self.metadata();record=meta['statements'][0]
        update_statement(meta,record['statement_id'],'inactive',timestamp='t')
        result=_issue_prompt_history([{'role':'user','content':'继续'}],'consult',meta,[],{'paragraph':{'text':'读'*6000}})
        self.assertTrue(any('本议题可纠正的信息' in x['content'] and 'inactive' in x['content'] for x in result))
        self.assertTrue(any('用户本轮选择讨论的资料' in x['content'] for x in result))
        self.assertEqual(result[0]['content'],'继续')
