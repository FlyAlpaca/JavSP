"""数据库原名（旧艺名）的安全匹配及用户报告样本。"""

import pytest
from lxml import etree

from javsp.config import load_config
from javsp.datatype import Movie, MovieInfo
from javsp.metadata.actors import ActorMapping
from javsp.metadata.summarizer import MetadataMerger
from javsp.publish.naming import generate_names
from javsp.publish.nfo import write_nfo
from javsp.workflow.models import CrawlResult

XML = '''<actor-mapping>
  <a jp="二階堂麗" zh_cn="二階堂麗" keyword="二阶堂丽,红音丽,あかね麗" />
  <a jp="藤浦めぐ" zh_cn="藤浦惠" keyword="めぐり" />
  <a jp="森沢かな" zh_cn="森泽佳奈" keyword="飯岡かなこ" />
  <a jp="彩月七緒" zh_cn="彩月七緒" />
  <a jp="蒼山愛奈" zh_cn="蒼山愛奈" />
  <a jp="雛形みくる" zh_cn="雛形みくる" keyword="雏形美久琉" />
</actor-mapping>'''.encode()


@pytest.mark.parametrize(('name', 'expected', 'alias_off'), [
    ('あかね麗（二階堂麗）', '二階堂麗（二阶堂丽）', 'あかね麗（二阶堂丽）'),
    ('めぐり（藤浦めぐ）', '藤浦めぐ（藤浦惠）', 'めぐり（藤浦惠）'),
    ('森沢かな（飯岡かなこ）', '森沢かな（森泽佳奈）', '森沢かな（森泽佳奈）'),
    (' めぐり ( 藤浦めぐ ) ', '藤浦めぐ（藤浦惠）', 'めぐり（藤浦惠）'),
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
    'めぐり（不明）', '不明（藤浦めぐ）', 'めぐり（森沢かな）',
    'めぐり（藤浦めぐ）追加', 'めぐり（藤浦めぐ)', 'めぐり(藤浦めぐ）',
    'めぐり（藤浦めぐ（旧名））', 'めぐり（）', '（藤浦めぐ）',
    'めぐり（共用）', '共用（藤浦めぐ）',
])
def test_uncertain_or_malformed_compound_is_preserved(name):
    mapping = ActorMapping.from_xml(XML.replace(b'</actor-mapping>', '''
      <a jp="藤浦めぐ" zh_cn="藤浦惠" keyword="共用" />
      <a jp="森沢かな" zh_cn="森泽佳奈" keyword="共用" />
    </actor-mapping>'''.encode()))
    assert mapping.resolve(name) == name
    assert mapping.resolve(name, normalize_aliases=False) == name


def test_exact_compound_entry_has_priority_over_splitting():
    mapping = ActorMapping.from_xml(XML.replace(b'</actor-mapping>', '''
      <a jp="登録名" zh_cn="登记名" keyword="めぐり（藤浦めぐ）" />
    </actor-mapping>'''.encode()))
    assert mapping.resolve('めぐり（藤浦めぐ）') == '登録名（登记名）'


def test_compound_does_not_bypass_conflicting_translations():
    mapping = ActorMapping.from_xml(XML.replace(b'</actor-mapping>', '''
      <a jp="藤浦めぐ" zh_cn="冲突译名" />
    </actor-mapping>'''.encode()))
    assert mapping.resolve('めぐり（藤浦めぐ）') == 'めぐり（藤浦めぐ）'


def test_verified_spelling_does_not_override_new_upstream_translation():
    mapping = ActorMapping.from_xml(XML.replace('zh_cn="彩月七緒"'.encode(), 'zh_cn="新译名"'.encode()))
    assert mapping.resolve('彩月七緒') == '彩月七緒（新译名）'


def test_compound_names_share_directory_nfo_and_portrait_keys(tmp_path):
    movie = Movie('ABC-123')
    movie.files = [str(tmp_path / 'ABC-123.mp4')]
    names = ['森沢かな（飯岡かなこ）', '森沢かな', '飯岡かなこ']
    source = MovieInfo(movie, title='title', cover='cover', actress=names,
                       actress_pics={names[0]: 'portrait'})
    cfg = load_config()
    result = CrawlResult({'r18dev': source}, [], movie.dvdid, None, 'normal')
    merged = MetadataMerger(cfg, aliases=ActorMapping.from_xml(XML)).merge(movie, result)
    display = '森沢かな（森泽佳奈）'
    assert merged.actress == [display]
    assert merged.actress_pics == {display: 'portrait'}
    assert source.actress == names and source.actress_pics == {names[0]: 'portrait'}
    movie.info = merged
    generate_names(movie, cfg)
    assert display in movie.save_dir
    target = tmp_path / 'movie.nfo'
    write_nfo(merged, target, cfg)
    tree = etree.parse(str(target))
    assert tree.xpath('/movie/actor/name/text()') == [display]
    assert tree.xpath('/movie/actor/thumb/text()') == ['portrait']
