"""上游中文字段、汉字别名回退及完整姓名匹配回归。"""

import pytest

from javsp.metadata.actors import ActorMapping

XML = '''<actor-mapping>
  <a jp="二階堂麗" zh_cn="二階堂麗" keyword="二阶堂丽,红音丽,あかね麗" />
  <a jp="藤浦めぐ" zh_cn="藤浦惠" keyword="めぐり" />
  <a jp="森沢かな" zh_cn="森泽佳奈" keyword="飯岡かなこ" />
  <a jp="彩月七緒" zh_cn="彩月七緒" />
  <a jp="蒼山愛奈" zh_cn="蒼山愛奈" />
  <a jp="雛形みくる" zh_cn="雛形みくる" keyword="雏形美久琉" />
  <a jp="神宮寺ナオ" zh_cn="神宮寺ナオ" zh_tw="神宮寺ナオ" keyword="松岡芽郁,池田充希,神宮寺奈緒" />
</actor-mapping>'''.encode()


@pytest.mark.parametrize(('name', 'expected', 'alias_off'), [
    ('あかね麗', '二階堂麗（二阶堂丽）', 'あかね麗（二阶堂丽）'),
    ('めぐり', '藤浦めぐ（藤浦惠）', 'めぐり（藤浦惠）'),
    ('森沢かな', '森沢かな（森泽佳奈）', '森沢かな（森泽佳奈）'),
    ('彩月七緒', '彩月七緒（彩月七绪）', '彩月七緒（彩月七绪）'),
    ('蒼山愛奈', '蒼山愛奈（苍山爱奈）', '蒼山愛奈（苍山爱奈）'),
    ('雛形みくる', '雛形みくる（雏形美久琉）', '雛形みくる（雏形美久琉）'),
    ('神宮寺ナオ', '神宮寺ナオ（神宫寺奈绪）', '神宮寺ナオ（神宫寺奈绪）'),
    ('神宮寺奈緒', '神宮寺ナオ（神宫寺奈绪）', '神宮寺奈緒（神宫寺奈绪）'),
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


def test_simplification_does_not_override_new_upstream_translation():
    mapping = ActorMapping.from_xml(XML.replace('zh_cn="彩月七緒"'.encode(), 'zh_cn="新译名"'.encode()))
    assert mapping.resolve('彩月七緒') == '彩月七緒（新译名）'


@pytest.mark.parametrize(('rows', 'expected'), [
    ('<a jp="神宮寺ナオ" />', '神宮寺ナオ'),
    ('<a jp="神宮寺ナオ" zh_cn="上游译名" keyword="神宮寺奈緒" />', '神宮寺ナオ（上游译名）'),
    ('<a jp="神宮寺ナオ" zh_cn="神宮寺ナオ" zh_tw="神宮寺奈緒" keyword="神宮寺奈緒" />', '神宮寺ナオ（神宮寺奈緒）'),
    ('<a jp="神宮寺ナオ" zh_cn="译名甲" keyword="神宮寺奈緒" /><a jp="神宮寺ナオ" zh_cn="译名乙" />', '神宮寺ナオ'),
    ('<a jp="其他原名" />', '神宮寺ナオ'),
])
def test_jinguji_translation_requires_upstream_evidence(rows, expected):
    mapping = ActorMapping.from_xml(f'<actor-mapping>{rows}</actor-mapping>'.encode())
    assert mapping.resolve('神宮寺ナオ') == expected


@pytest.mark.parametrize(('jp', 'keywords', 'expected'), [
    ('架空あおい', '旧名花子,架空葵', '架空あおい（架空葵）'),
    ('白花にあ', '白花仁愛,白花仁爱', '白花にあ（白花仁爱）'),
    ('愛川すず', '爱川铃,愛川鈴', '愛川すず（爱川铃）'),
    ('白石まゆ', '白石真由,白石麻友', '白石まゆ'),
    ('白石まゆ', '前白石真由,別姓真由,白石mayu,白石ま由', '白石まゆ'),
    ('白石まゆ', '白石', '白石まゆ'),
    ('あおい', '葵', 'あおい'),
    ('葵あおい', '葵青衣', '葵あおい'),
    ('架空・あおい', '架空葵', '架空・あおい'),
    ('架空Aあおい', '架空葵', '架空Aあおい'),
    ('架空あお美', '架空青美,架空青子', '架空あお美（架空青美）'),
    ('架あお空', '架青空,架青子', '架あお空（架青空）'),
])
def test_keyword_fallback_matches_kanji_segments_without_name_overrides(jp, keywords, expected):
    mapping = ActorMapping.from_xml(
        f'<actor-mapping><a jp="{jp}" zh_cn="{jp}" keyword="{keywords}" /></actor-mapping>'.encode()
    )
    assert mapping.resolve(jp) == expected
    assert mapping.resolve(jp, normalize_aliases=False) == expected
    assert mapping.resolve(expected) == expected


@pytest.mark.parametrize('other', [
    '<a jp="別人" keyword="白花仁爱" />',
    '<a jp="別人" zh_cn="白花仁爱" />',
    '<a jp="白花仁爱" />',
])
def test_simplified_alias_ownership_is_checked_across_all_indexes(other):
    mapping = ActorMapping.from_xml(f'''<actor-mapping>
      <a jp="白花にあ" keyword="白花仁愛" />{other}
    </actor-mapping>'''.encode())
    assert mapping.resolve('白花にあ') == '白花にあ'


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize(('second', 'expected'), [
    ('白花仁爱', '白花にあ（白花仁爱）'),
    ('白花新名', '白花にあ'),
])
def test_duplicate_rows_merge_equivalent_candidates_but_preserve_conflicts(reverse, second, expected):
    rows = ['<a jp="白花にあ" keyword="白花仁愛" />', f'<a jp="白花にあ" keyword="{second}" />']
    if reverse:
        rows.reverse()
    mapping = ActorMapping.from_xml(('<actor-mapping>' + ''.join(rows) + '</actor-mapping>').encode())
    assert mapping.resolve('白花にあ') == expected


def test_kanji_placeholder_is_simplified_without_inferring_unmapped_names():
    mapping = ActorMapping.from_xml('''<actor-mapping>
      <a jp="架空麗華" zh_cn="架空麗華" />
      <a jp="架空優華" />
    </actor-mapping>'''.encode())
    assert mapping.resolve('架空麗華') == '架空麗華（架空丽华）'
    assert mapping.resolve('架空優華') == '架空優華'


def test_kanji_placeholder_simplification_respects_ownership_conflicts():
    mapping = ActorMapping.from_xml('''<actor-mapping>
      <a jp="架空麗華" zh_cn="架空麗華" />
      <a jp="其他原名" keyword="架空丽华" />
    </actor-mapping>'''.encode())
    assert mapping.resolve('架空麗華') == '架空麗華'


def test_kanji_name_with_missing_chinese_requires_equivalent_keyword():
    mapping = ActorMapping.from_xml('''<actor-mapping>
      <a jp="架空麗華" keyword="架空丽华,其他旧名" />
      <a jp="架空優華" keyword="其他旧名" />
    </actor-mapping>'''.encode())
    assert mapping.resolve('架空麗華') == '架空麗華（架空丽华）'
    assert mapping.resolve('架空優華') == '架空優華'
