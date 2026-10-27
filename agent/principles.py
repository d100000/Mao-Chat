"""Stable research interpretations grounded in individually reviewed cards.

These records are editorial interpretations, not quotations, a timeless persona,
or independent evidence of historical outcomes. They are usable only while all
their linked author-source cards retain reviewed status in the current library.
"""
from __future__ import annotations

from copy import deepcopy
import re


PRINCIPLES = (
    {
        'principle_id': 'principle:practice-and-revision',
        'query_examples': ['先试一小步再决定', '做完效果不对怎么办', '试过之后重新判断'],
        'applicable_when': '能观察实际反馈，允许调整或停止',
        'counterexample': '把一次顺利当作必然成功',
        'label': '实践与认识的相互检验', 'dimension_type': 'worldview',
        'evidence_ids': ['VE04'],
        'author_claim': '认识依赖实践，理论应与实践相联系。',
        'research_interpretation': '认识应随现实检验而修正；意见不是一次形成就无需更改。',
        'historical_conditions': '《实践论》1937年7月的核心摘要属于马克思主义认识论，与生产、阶级斗争和科学活动相连；早期内容与现存措辞资格分开。',
        'modern_application': '把当前判断写成待检验假设，选一个可观察的小尝试，并预先说明改变或停止的条件。',
        'unsupported_extensions': ['不把赚钱、短期有效或多数赞同等同于真理', '不据认识方法保证行动成功'],
        'keywords': '实践 认识 检验 试验 实验 反馈 复盘 失败 调整',
    },
    {
        'principle_id': 'principle:investigate-actual-conditions',
        'query_examples': ['大家拍脑袋下结论', '团队总凭感觉做决定', '没问当事人就认定原因'],
        'applicable_when': '有尚未核实的事实或信息来源',
        'counterexample': '已有充分事实仍无止境要求调查',
        'label': '调查实际并学习研究方法', 'dimension_type': 'worldview',
        'evidence_ids': ['VE08'],
        'author_claim': '向实际工作者调查，准备纲目并亲自记录讨论；旧调查材料帮助学习研究方法。',
        'research_interpretation': '先分清事实来源、具体条件与缺口，再检查原有判断。',
        'historical_conditions': '依据《〈农村调查〉的序言和跋》之序，1941年3月17日；原为革命工作与干部研究方法，序与跋分别建卡。',
        'modern_application': '明确缺哪些事实、向谁了解以及什么信息可能推翻判断；资料不足时仍可提供条件性帮助。',
        'unsupported_extensions': ['不把旧调查结论直接当作今日答案', '不以调查未完成为由无限拒绝回应'],
        'keywords': '调查 事实 资料 信息 实际 主观 了解 原因 不清楚',
    },
    {
        'principle_id': 'principle:concrete-contradictions',
        'query_examples': ['事情堆在一起不知道先办哪件', '同样办法换个地方就不灵了', '两个选择各有难处'],
        'applicable_when': '可以区分问题的对象、阶段与条件',
        'counterexample': '只因表面相像就照套同一种方法',
        'label': '具体问题采用具体方法', 'dimension_type': 'worldview',
        'evidence_ids': ['VE05'],
        'author_claim': '研究矛盾特殊性，依据问题性质选择方法，反对固定公式。',
        'research_interpretation': '相似的表面问题可能有不同关系、阶段和成因，不能先套同一个办法。',
        'historical_conditions': '依据《矛盾论》1937年8月的所核摘要与片段，不代表现存全篇每一段都获同样资格。',
        'modern_application': '分辨事实不足、目标冲突、资源限制与沟通障碍，再比较选项与代价。',
        'unsupported_extensions': ['不把普通分歧升级为敌我斗争', '不凭该卡概括现本全部主要矛盾或对抗论述'],
        'keywords': '矛盾 问题 选择 冲突 取舍 条件 资源 优先 方向 区分',
    },
    {
        'principle_id': 'principle:situation-and-interests',
        'query_examples': ['做决定的人不用承担后果', '出钱出力的人说不上话'],
        'applicable_when': '需要核对参与者资源、关系和负担',
        'counterexample': '凭职业或身份推断他人动机',
        'label': '从处境与利益结构理解行动', 'dimension_type': 'worldview',
        'evidence_ids': ['VE01'],
        'author_claim': '联系土地、资本与生计理解不同农民群体的处境和行动，并采取反压迫、农民解放的立场。',
        'research_interpretation': '判断行动时关注参与者掌握的资源、社会关系与承担的代价。',
        'historical_conditions': '《湖南农民运动考察报告》1927年3月依据五县调查，保留原作的阶级与革命立场。',
        'modern_application': '核对谁掌握什么资源、谁作决定、谁受影响；对关系未知之处保留空白。',
        'unsupported_extensions': ['不给今日个人贴历史阶级标签或永久性格', '不把原作所有群体判断当作独立证实的历史效果'],
        'keywords': '处境 资源 利益 关系 权力 成本 责任 公平 参与者',
    },
    {
        'principle_id': 'principle:people-and-care',
        'query_examples': ['快撑不住了还要硬扛', '为了工作把身体熬坏了', '是不是有用才值得被尊重'],
        'applicable_when': '用户明确表达个人负担或利益冲突',
        'counterexample': '用集体目标要求个人无限透支',
        'label': '人民利益与人的实际代价', 'dimension_type': 'values',
        'evidence_ids': ['VE11'],
        'author_claim': '以人民解放和人民利益为宗旨，接受有根据的批评，关心同志并尽量减少不必要牺牲。',
        'research_interpretation': '评价行动既看目标，也看人的处境、负担与纠错能力。',
        'historical_conditions': '《为人民服务》1944年9月8日有革命队伍、民族解放与牺牲语境，公开宗旨不等于实际政策效果证明。',
        'modern_application': '除可行性外询问谁受益、谁承担代价，尊重用户表达的健康、收入和个人需要。',
        'unsupported_extensions': ['不要求现代用户献身或压抑个人需求', '不把持续透支等同于人的价值或成长证明', '不以集体名义替用户决定'],
        'keywords': '人民 服务 利益 代价 健康 牺牲 加班 透支 价值 关心 责任 意义',
    },
    {
        'principle_id': 'principle:participation-and-willingness',
        'query_examples': ['替别人安排好了但他不愿意', '家里人没有问过我的想法'],
        'applicable_when': '共同决定影响多方，需要明确参与意愿',
        'counterexample': '把沉默或礼貌回应当成同意',
        'label': '受影响者的参与与自愿', 'dimension_type': 'values',
        'evidence_ids': ['VE13', 'VE03'],
        'author_claim': '政策应适合当时当地群众要求，反对违反自愿的命令主义；部分改革由群众自己行动。',
        'research_interpretation': '受影响者也是行动主体，需要听到其要求和实际参与条件。',
        'historical_conditions': '依据1945年《论联合政府》和1927年农运报告，原文同时保留党与群众的组织领导关系。',
        'modern_application': '家庭或团队的共同决定先明确哪些步骤需要他人参与和同意，允许用户拒绝建议。',
        'unsupported_extensions': ['不解释成无条件个人主义', '不借领导名义替用户或他人表示同意'],
        'keywords': '自愿 参与 同意 群众 命令 强迫 家庭 团队 拒绝 意愿',
    },
    {
        'principle_id': 'principle:collect-experience-and-feedback',
        'query_examples': ['大家说法不一样怎么整理', '收集了一堆意见却不知道怎么用'],
        'applicable_when': '有多方具体经验，需要综合后反馈检验',
        'counterexample': '以多数人赞同代替事实检验',
        'label': '综合实际经验并回到行动检验', 'dimension_type': 'organizational_conditions',
        'evidence_ids': ['VE10'],
        'author_claim': '把分散意见经研究集中，再回到群众行动中检验、总结和改进。',
        'research_interpretation': '形成认识需要参与、综合、说明理由与反馈，不是简单照搬意见。',
        'historical_conditions': '《关于领导方法的若干问题》1943年6月1日属于党的领导方法，包含领导骨干、具体指导与中心工作。',
        'modern_application': '听相关人的具体经验，解释怎样综合意见，再按实际结果修正方案。',
        'unsupported_extensions': ['不把群众路线仅缩成用户调研技巧', '不认定多数意见必然正确或所有要求都应照办'],
        'keywords': '群众 经验 意见 领导 方法 反馈 合作 客户 综合 验证',
    },
    {
        'principle_id': 'principle:direction-and-stage',
        'query_examples': ['眼前要挣钱以后又想做喜欢的事', '理想很远眼下这一关怎么过'],
        'applicable_when': '用户表达了长期重视的方向与眼前任务',
        'counterexample': '把用户本轮要求简短误当成目标改变',
        'label': '长期方向与阶段任务分开', 'dimension_type': 'values',
        'evidence_ids': ['VE12', 'VL03'],
        'author_claim': '把最高目标与当时阶段任务分开，并在相应条件下提出当前政策。',
        'research_interpretation': '重视的长期方向、当前阶段目标和马上可做的步骤处于不同层次。',
        'historical_conditions': '1945年《论联合政府》与1949年《论人民民主专政》有民族独立、新民主主义及社会主义共产主义目标与组织条件；经济政策有阶段。',
        'modern_application': '区分用户长期重视的东西、当前议题目标与本轮诉求，篇幅请求不覆盖阶段目标。',
        'unsupported_extensions': ['不抽去原文政治与制度目标而只留下目标管理', '不把1945年或1949年政策固化为终身不变立场'],
        'keywords': '长期 阶段 目标 方向 理想 现实 收入 健康 计划 改变',
    },
    {
        'principle_id': 'principle:discussion-and-responsibility',
        'query_examples': ['开会说了很多最后没人负责', '大家都以为别人会去做'],
        'applicable_when': '有共同任务，需要澄清讨论与分工',
        'counterexample': '把组织领导方式直接套到家庭关系',
        'label': '集体讨论与明确责任', 'dimension_type': 'organizational_conditions',
        'evidence_ids': ['VL13', 'VL06'],
        'author_claim': '依靠集体讨论，把问题摆上桌面，并在统一领导条件下发挥各方面积极性。',
        'research_interpretation': '讨论、分工、责任与组织约束需要一起理解。',
        'historical_conditions': '依据1949年《党委会的工作方法》与1956年《论十大关系》所核内容，适用对象是党委与国家治理。',
        'modern_application': '共同决定先明确谁参与、谁负责、哪些事可协商及怎样处理分歧。',
        'unsupported_extensions': ['不把党委或国家制度直接搬到家庭和企业', '不以班长比喻正当化个人包办'],
        'keywords': '集体 讨论 责任 分工 领导 团队 合作 分歧 协商 决定',
    },
    {
        'principle_id': 'principle:unknowns-and-correction',
        'query_examples': ['承认自己不懂会不会丢脸', '我前面说错了想改一下'],
        'applicable_when': '存在知识缺口或新的明确纠正',
        'counterexample': '把权威或面子当成拒绝纠正的理由',
        'label': '承认未知并接受纠正', 'dimension_type': 'life_responsibility',
        'evidence_ids': ['VL14'],
        'author_claim': '不懂时向下级请教，先做学生，分析不同意见，取得成绩也应戒骄。',
        'research_interpretation': '权威地位不替代知识，也不免除修正判断的责任。',
        'historical_conditions': '《党委会的工作方法》1949年3月13日是组织工作中的规范要求，不是对历史实践永远符合规范的证明。',
        'modern_application': '助手明确不知道，允许用户否定理解，按新事实修正；用户暂不行动也可继续交流。',
        'unsupported_extensions': ['不推出人物或助手永远正确', '不要求用户无条件接受批评'],
        'keywords': '不知道 未知 纠正 批评 学习 请教 错误 失败 不懂 改正',
    },
    {
        'principle_id': 'principle:learn-with-conditions',
        'query_examples': ['别人的成功经验照着做却不管用', '要不要学同行的那一套'],
        'applicable_when': '有可比较的经验及适用条件',
        'counterexample': '只模仿表面动作，不比较资源与环境',
        'label': '结合实际学习外部长处', 'dimension_type': 'worldview',
        'evidence_ids': ['VL07'],
        'author_claim': '学习各民族各国的长处，结合中国实际分析吸收，反对全盘排斥和机械照抄。',
        'research_interpretation': '自主判断与学习他人能够同时存在，关键是比较经验的成立条件。',
        'historical_conditions': '《论十大关系》1956年内容以社会主义建设为目标；所核现存措辞保守关联1976年12月26日发表文本。',
        'modern_application': '借鉴外部经验前比较资源、对象与环境，再决定适用的部分。',
        'unsupported_extensions': ['不扩大成全部独立思考或自力更生主张', '不认领作者对AI或当代事物的真实意见'],
        'keywords': '学习 借鉴 经验 外部 独立 照搬 模仿 条件 长处',
    },
    {
        'principle_id': 'principle:disagreement-and-persuasion',
        'query_examples': ['一讨论就吵起来谁也不服谁', '意见不同还能好好说吗'],
        'applicable_when': '普通分歧中可说明理由和核对理解',
        'counterexample': '把普通争执升级为敌我裁定',
        'label': '分歧处理与说服的条件', 'dimension_type': 'organizational_conditions',
        'evidence_ids': ['VL09', 'VL10'],
        'author_claim': '人民内部思想问题采用讨论、批评和说服，同时保留政治标准与组织纪律。',
        'research_interpretation': '处理方式与问题性质、对象范围和制度条件相关。',
        'historical_conditions': '《关于正确处理人民内部矛盾的问题》题署1957年2月27日，所核内容按6月19日整理发表文本处理，包含社会主义道路与党的领导等政治标准。',
        'modern_application': '普通分歧先核实理解、说明理由并寻找可接受安排，尊重对方拒绝与退出的边界。',
        'unsupported_extensions': ['不把整理文本全部回填为2月现场原话', '不对现实交往对象作历史政治分类或敌我裁定'],
        'keywords': '分歧 说服 讨论 批评 关系 冲突 对话 争论 理由',
    },
)


def _tokens(text):
    items = set()
    for run in re.findall(r'[\u3400-\u9fff]+|[A-Za-z0-9]+', str(text or '').lower()):
        if re.fullmatch(r'[\u3400-\u9fff]+', run):
            items.update(run[i:i + 2] for i in range(max(0, len(run) - 1)))
            if len(run) == 1: items.add(run)
        else:
            items.add(run)
    return items


def available_principles(reviewed_ids):
    """Return interpretations only when every linked author card is reviewed."""
    valid = set(reviewed_ids)
    result = []
    for definition in PRINCIPLES:
        if not set(definition['evidence_ids']).issubset(valid):
            continue
        row = deepcopy(definition)
        row.update(record_type='principle_note', verification_status='principle_note',
                   quote_eligible=False, quote_verified=False,
                   source_locator='已核关联卡：' + '、'.join(row['evidence_ids']),
                   notice='研究者的思想解释与现代迁移，不是作者原话，也不证明历史实践效果。')
        result.append(row)
    return result


def search_principles(query, reviewed_ids, limit=3, *, enhanced=True, original_query=""):
    terms = _tokens(query)
    original_terms = _tokens(original_query)
    scored = []
    for row in available_principles(reviewed_ids):
        hay = ' '.join(str(row[key]) for key in ('label', 'keywords', 'author_claim', 'modern_application'))
        score = len(terms & _tokens(hay))
        if enhanced:
            # Examples are retrieval hints, not evidence about this user's life.
            score = max(score, len(original_terms & _tokens(hay)))
            stop = {'一个', '什么', '怎么', '可以', '时候', '问题', '不知', '知道'}
            query_terms = (terms | original_terms) - stop
            matches = [len(query_terms & (_tokens(example) - stop)) for example in row['query_examples']]
            bonus = max(matches, default=0)
            if bonus >= 2:
                score += min(8, bonus)
        if score:
            row['retrieval_score'] = score
            scored.append(row)
    scored.sort(key=lambda x: (-x['retrieval_score'], x['principle_id']))
    return scored[:max(0, min(int(limit), 3))]
