"""已核实中文写法及完整姓名匹配回归。"""

import pytest

from javsp.metadata.actors import ActorMapping

XML = '''<actor-mapping>
  <a jp="二階堂麗" zh_cn="二階堂麗" keyword="二阶堂丽,红音丽,あかね麗" />
  <a jp="藤浦めぐ" zh_cn="藤浦惠" keyword="めぐり" />
  <a jp="森沢かな" zh_cn="森泽佳奈" keyword="飯岡かなこ" />
  <a jp="彩月七緒" zh_cn="彩月七緒" />
  <a jp="蒼山愛奈" zh_cn="蒼山愛奈" />
  <a jp="雛形みくる" zh_cn="雛形みくる" keyword="雏形美久琉" />
</actor-mapping>'''.encode()


@pytest.mark.parametrize(('name', 'expected', 'alias_off'), [
    ('あかね麗', '二階堂麗（二阶堂丽）', 'あかね麗（二阶堂丽）'),
    ('めぐり', '藤浦めぐ（藤浦惠）', 'めぐり（藤浦惠）'),
    ('森沢かな', '森沢かな（森泽佳奈）', '森沢かな（森泽佳奈）'),
    ('彩月七緒', '彩月七緒（彩月七绪）', '彩月七緒（彩月七绪）'),
    ('蒼山愛奈', '蒼山愛奈（苍山爱奈）', '蒼山愛奈（苍山爱奈）'),
    ('雛形みくる', '雛形みくる（雏形美久琉）', '雛形みくる（雏形美久琉）'),
    ('サンディー・雛・パークス', 'サンディー・雛・パークス', 'サンディー・雛・パークス'),
])
def test_reported_names(name, expected, alias_off):
    mapping = ActorMapping.from_xml(XML)
    for normalize, result in ((True, expected), (False, alias_off)):
        assert mapping.resolve(name, normalize_aliases=normalize) == result
        assert mapping.resolve(result, normalize_aliases=normalize) == result


@pytest.mark.parametrize('name', [
    'あかね麗（二階堂麗）', 'めぐり（藤浦めぐ）', '森沢かな（飯岡かなこ）',
    'めぐり(藤浦めぐ)',
])
def test_unmatched_full_name_is_preserved(name):
    mapping = ActorMapping.from_xml(XML)
    assert mapping.resolve(name) == name
    assert mapping.resolve(name, normalize_aliases=False) == name


def test_verified_spelling_does_not_override_new_upstream_translation():
    mapping = ActorMapping.from_xml(XML.replace('zh_cn="彩月七緒"'.encode(), 'zh_cn="新译名"'.encode()))
    assert mapping.resolve('彩月七緒') == '彩月七緒（新译名）'
