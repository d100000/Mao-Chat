import unittest

from agent.dialogue import format_violations


class FormatParagraphTests(unittest.TestCase):
    def test_three_natural_sentences_can_have_paragraph_breaks(self):
        text = '先核实客户的实际需求。\n\n本周只记录具体反馈，不作交付承诺。\n有反馈再决定继续还是调整。'
        self.assertEqual(format_violations(text, {'sentences': 3, 'max_chars': 200}), [])

    def test_extra_sentences_headings_lists_and_character_limits_still_apply(self):
        ordinary = '先核实需求。\n记录具体反馈。\n再决定是否继续。'
        for text in ['判断：\n' + ordinary, '**建议**\n' + ordinary,
                     '1. 先核实需求。\n2. 记录具体反馈。\n3. 再决定是否继续。',
                     ordinary + '\n另外再补充一句。']:
            with self.subTest(text=text):
                self.assertTrue(format_violations(text, {'sentences': 3, 'max_chars': 200}))
        self.assertIn('超过字数上限', format_violations(ordinary, {'sentences': 3, 'max_chars': 5}))


if __name__ == '__main__': unittest.main()
